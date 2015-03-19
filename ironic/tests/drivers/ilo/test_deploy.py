# Copyright 2014 Hewlett-Packard Development Company, L.P.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Test class for common methods used by iLO modules."""

import tempfile

import mock
from oslo_config import cfg

from ironic.common import boot_devices
from ironic.common import exception
from ironic.common.glance_service import service_utils
from ironic.common import image_service
from ironic.common import images
from ironic.common import keystone
from ironic.common import states
from ironic.common import swift
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.drivers.modules import agent
from ironic.drivers.modules import agent_base_vendor
from ironic.drivers.modules import deploy_utils
from ironic.drivers.modules.ilo import common as ilo_common
from ironic.drivers.modules.ilo import deploy as ilo_deploy
from ironic.drivers.modules import iscsi_deploy
from ironic.drivers.modules import pxe
from ironic.drivers import utils as driver_utils
from ironic.tests.conductor import utils as mgr_utils
from ironic.tests.db import base as db_base
from ironic.tests.db import utils as db_utils
from ironic.tests.objects import utils as obj_utils


INFO_DICT = db_utils.get_test_ilo_info()
CONF = cfg.CONF


class IloDeployPrivateMethodsTestCase(db_base.DbTestCase):

    def setUp(self):
        super(IloDeployPrivateMethodsTestCase, self).setUp()
        mgr_utils.mock_the_extension_manager(driver="iscsi_ilo")
        self.node = obj_utils.create_test_node(self.context,
                driver='iscsi_ilo', driver_info=INFO_DICT)

    def test__get_boot_iso_object_name(self):
        boot_iso_actual = ilo_deploy._get_boot_iso_object_name(self.node)
        boot_iso_expected = "boot-%s" % self.node.uuid
        self.assertEqual(boot_iso_expected, boot_iso_actual)

    @mock.patch.object(image_service.HttpImageService, 'validate_href')
    def test__get_boot_iso_http_url(self, service_mock):
        url = 'http://abc.org/image/qcow2'
        i_info = self.node.instance_info
        i_info['ilo_boot_iso'] = url
        self.node.instance_info = i_info
        self.node.save()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            boot_iso_actual = ilo_deploy._get_boot_iso(task, 'root-uuid')
            service_mock.assert_called_once_with(url)
            self.assertEqual(url, boot_iso_actual)

    @mock.patch.object(image_service.HttpImageService, 'validate_href')
    def test__get_boot_iso_url(self, mock_validate):
        url = 'http://aaa/bbb'
        i_info = self.node.instance_info
        i_info['ilo_boot_iso'] = url
        self.node.instance_info = i_info
        self.node.save()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            boot_iso_actual = ilo_deploy._get_boot_iso(task, 'root-uuid')
            self.assertEqual(url, boot_iso_actual)
            mock_validate.assert_called_ince_with(url)

    @mock.patch.object(image_service.HttpImageService, 'validate_href')
    def test__get_boot_iso_unsupported_url(self, validate_href_mock):
        validate_href_mock.side_effect = exception.ImageRefValidationFailed(
            image_href='file://img.qcow2', reason='fail')
        url = 'file://img.qcow2'
        i_info = self.node.instance_info
        i_info['ilo_boot_iso'] = url
        self.node.instance_info = i_info
        self.node.save()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            self.assertRaises(exception.ImageRefValidationFailed,
                              ilo_deploy._get_boot_iso, task, 'root-uuid')

    @mock.patch.object(images, 'get_image_properties')
    @mock.patch.object(ilo_deploy, '_parse_deploy_info')
    def test__get_boot_iso_glance_image(self, deploy_info_mock,
            image_props_mock):
        deploy_info_mock.return_value = {'image_source': 'image-uuid',
                                         'ilo_deploy_iso': 'deploy_iso_uuid'}
        image_props_mock.return_value = {'boot_iso': 'boot-iso-uuid',
                                         'kernel_id': None,
                                         'ramdisk_id': None}

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            boot_iso_actual = ilo_deploy._get_boot_iso(task, 'root-uuid')
            deploy_info_mock.assert_called_once_with(task.node)
            image_props_mock.assert_called_once_with(
                task.context, 'image-uuid',
                ['boot_iso', 'kernel_id', 'ramdisk_id'])
            boot_iso_expected = 'boot-iso-uuid'
            self.assertEqual(boot_iso_expected, boot_iso_actual)

    @mock.patch.object(driver_utils, 'get_node_capability')
    @mock.patch.object(images, 'get_image_properties')
    @mock.patch.object(ilo_deploy, '_parse_deploy_info')
    def test__get_boot_iso_uefi_no_glance_image(self, deploy_info_mock,
            image_props_mock, get_node_cap_mock):
        deploy_info_mock.return_value = {'image_source': 'image-uuid',
                                         'ilo_deploy_iso': 'deploy_iso_uuid'}
        image_props_mock.return_value = {'boot_iso': None,
                                         'kernel_id': None,
                                         'ramdisk_id': None}
        get_node_cap_mock.return_value = 'uefi'

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            boot_iso_result = ilo_deploy._get_boot_iso(task, 'root-uuid')
            deploy_info_mock.assert_called_once_with(task.node)
            image_props_mock.assert_called_once_with(
                task.context, 'image-uuid',
                ['boot_iso', 'kernel_id', 'ramdisk_id'])
            get_node_cap_mock.assert_not_called(task.node, 'boot_mode')
            self.assertIsNone(boot_iso_result)

    @mock.patch.object(tempfile, 'NamedTemporaryFile')
    @mock.patch.object(images, 'create_boot_iso')
    @mock.patch.object(swift, 'SwiftAPI')
    @mock.patch.object(ilo_deploy, '_get_boot_iso_object_name')
    @mock.patch.object(driver_utils, 'get_node_capability')
    @mock.patch.object(images, 'get_image_properties')
    @mock.patch.object(ilo_deploy, '_parse_deploy_info')
    def test__get_boot_iso_create(self, deploy_info_mock, image_props_mock,
                                  capability_mock, boot_object_name_mock,
                                  swift_api_mock,
                                  create_boot_iso_mock, tempfile_mock):
        CONF.keystone_authtoken.auth_uri = 'http://authurl'
        CONF.ilo.swift_ilo_container = 'ilo-cont'
        CONF.pxe.pxe_append_params = 'kernel-params'

        swift_obj_mock = swift_api_mock.return_value
        fileobj_mock = mock.MagicMock()
        fileobj_mock.name = 'tmpfile'
        mock_file_handle = mock.MagicMock(spec=file)
        mock_file_handle.__enter__.return_value = fileobj_mock
        tempfile_mock.return_value = mock_file_handle

        deploy_info_mock.return_value = {'image_source': 'image-uuid',
                                         'ilo_deploy_iso': 'deploy_iso_uuid'}
        image_props_mock.return_value = {'boot_iso': None,
                                         'kernel_id': 'kernel_uuid',
                                         'ramdisk_id': 'ramdisk_uuid'}
        boot_object_name_mock.return_value = 'abcdef'
        create_boot_iso_mock.return_value = '/path/to/boot-iso'
        capability_mock.return_value = 'uefi'

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            boot_iso_actual = ilo_deploy._get_boot_iso(task, 'root-uuid')
            deploy_info_mock.assert_called_once_with(task.node)
            image_props_mock.assert_called_once_with(task.context,
                'image-uuid', ['boot_iso', 'kernel_id', 'ramdisk_id'])
            boot_object_name_mock.assert_called_once_with(task.node)
            create_boot_iso_mock.assert_called_once_with(task.context,
                                                         'tmpfile',
                                                         'kernel_uuid',
                                                         'ramdisk_uuid',
                                                         'deploy_iso_uuid',
                                                         'root-uuid',
                                                         'kernel-params',
                                                         'uefi')
            swift_obj_mock.create_object.assert_called_once_with('ilo-cont',
                                                                 'abcdef',
                                                                 'tmpfile')
            boot_iso_expected = 'swift:abcdef'
            self.assertEqual(boot_iso_expected, boot_iso_actual)

    @mock.patch.object(ilo_deploy, '_get_boot_iso_object_name')
    @mock.patch.object(swift, 'SwiftAPI')
    def test__clean_up_boot_iso_for_instance(self, swift_mock,
                                             boot_object_name_mock):
        swift_obj_mock = swift_mock.return_value
        CONF.ilo.swift_ilo_container = 'ilo-cont'
        boot_object_name_mock.return_value = 'boot-object'
        i_info = self.node.instance_info
        i_info['ilo_boot_iso'] = 'swift:bootiso'
        self.node.instance_info = i_info
        self.node.save()
        ilo_deploy._clean_up_boot_iso_for_instance(self.node)
        swift_obj_mock.delete_object.assert_called_once_with('ilo-cont',
                                                             'boot-object')

    @mock.patch.object(ilo_deploy, '_get_boot_iso_object_name')
    def test__clean_up_boot_iso_for_instance_no_boot_iso(
            self, boot_object_name_mock):
        ilo_deploy._clean_up_boot_iso_for_instance(self.node)
        self.assertFalse(boot_object_name_mock.called)

    @mock.patch.object(deploy_utils, 'check_for_missing_params')
    def test__parse_driver_info(self, check_params_mock):
        self.node.driver_info['ilo_deploy_iso'] = 'deploy-iso-uuid'
        driver_info_expected = {'ilo_deploy_iso': 'deploy-iso-uuid'}
        driver_info_actual = ilo_deploy._parse_driver_info(self.node)
        error_msg = ("Error validating iLO virtual media deploy. Some"
                     " parameters were missing in node's driver_info")
        check_params_mock.assert_called_once_with(driver_info_expected,
                                                  error_msg)
        self.assertEqual(driver_info_expected, driver_info_actual)

    @mock.patch.object(ilo_deploy, '_parse_driver_info')
    @mock.patch.object(iscsi_deploy, 'parse_instance_info')
    def test__parse_deploy_info(self, instance_info_mock, driver_info_mock):
        instance_info_mock.return_value = {'a': 'b'}
        driver_info_mock.return_value = {'c': 'd'}
        expected_info = {'a': 'b', 'c': 'd'}
        actual_info = ilo_deploy._parse_deploy_info(self.node)
        self.assertEqual(expected_info, actual_info)

    @mock.patch.object(manager_utils, 'node_power_action')
    @mock.patch.object(manager_utils, 'node_set_boot_device')
    @mock.patch.object(ilo_common, 'setup_vmedia_for_boot')
    def test__reboot_into(self, setup_vmedia_mock, set_boot_device_mock,
                          node_power_action_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            opts = {'a': 'b'}
            ilo_deploy._reboot_into(task, 'iso', opts)
            setup_vmedia_mock.assert_called_once_with(task, 'iso', opts)
            set_boot_device_mock.assert_called_once_with(task,
                                                         boot_devices.CDROM)
            node_power_action_mock.assert_called_once_with(task, states.REBOOT)

    @mock.patch.object(ilo_deploy, '_reboot_into')
    @mock.patch.object(agent, 'build_agent_options')
    def test__prepare_agent_vmedia_boot(self, build_options_mock,
                                        reboot_into_mock):
        deploy_opts = {'a': 'b'}
        build_options_mock.return_value = deploy_opts
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.driver_info['ilo_deploy_iso'] = 'deploy-iso-uuid'
            ilo_deploy._prepare_agent_vmedia_boot(task)
            build_options_mock.assert_called_once_with(task.node)
            reboot_into_mock.assert_called_once_with(task,
                                                     'deploy-iso-uuid',
                                                     deploy_opts)


class IloVirtualMediaIscsiDeployTestCase(db_base.DbTestCase):

    def setUp(self):
        super(IloVirtualMediaIscsiDeployTestCase, self).setUp()
        mgr_utils.mock_the_extension_manager(driver="iscsi_ilo")
        self.node = obj_utils.create_test_node(self.context,
                driver='iscsi_ilo', driver_info=INFO_DICT)

    @mock.patch.object(driver_utils, 'validate_boot_mode_capability')
    @mock.patch.object(iscsi_deploy, 'validate_image_properties')
    @mock.patch.object(ilo_deploy, '_parse_deploy_info')
    @mock.patch.object(iscsi_deploy, 'validate')
    def _test_validate(self, validate_mock, deploy_info_mock,
                       validate_prop_mock, validate_boot_mode_mock,
                       props_expected):
        d_info = {'image_source': 'uuid'}
        deploy_info_mock.return_value = d_info
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.deploy.validate(task)
            validate_mock.assert_called_once_with(task)
            deploy_info_mock.assert_called_once_with(task.node)
            validate_prop_mock.assert_called_once_with(task.context,
                    d_info, props_expected)
            validate_boot_mode_mock.assert_called_once_with(task.node)

    @mock.patch.object(iscsi_deploy, 'validate_image_properties')
    @mock.patch.object(ilo_deploy, '_parse_deploy_info')
    @mock.patch.object(iscsi_deploy, 'validate')
    def test_validate_invalid_boot_option(self,
                                          validate_mock,
                                          deploy_info_mock,
                                          validate_prop_mock):
        d_info = {'image_source': 'uuid'}
        properties = {'capabilities': 'boot_mode:uefi,boot_option:foo'}
        deploy_info_mock.return_value = d_info
        props = ['kernel_id', 'ramdisk_id']
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.properties = properties
            exc = self.assertRaises(exception.InvalidParameterValue,
                                    task.driver.deploy.validate,
                                    task)
            validate_mock.assert_called_once_with(task)
            deploy_info_mock.assert_called_once_with(task.node)
            validate_prop_mock.assert_called_once_with(task.context,
                                                       d_info, props)
            self.assertIn('boot_option', str(exc))

    @mock.patch.object(iscsi_deploy, 'validate_image_properties')
    @mock.patch.object(ilo_deploy, '_parse_deploy_info')
    @mock.patch.object(iscsi_deploy, 'validate')
    def test_validate_invalid_boot_mode(self,
                                        validate_mock,
                                        deploy_info_mock,
                                        validate_prop_mock):
        d_info = {'image_source': 'uuid'}
        properties = {'capabilities': 'boot_mode:foo,boot_option:local'}
        deploy_info_mock.return_value = d_info
        props = ['kernel_id', 'ramdisk_id']
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.properties = properties
            exc = self.assertRaises(exception.InvalidParameterValue,
                                    task.driver.deploy.validate,
                                    task)
            validate_mock.assert_called_once_with(task)
            deploy_info_mock.assert_called_once_with(task.node)
            validate_prop_mock.assert_called_once_with(task.context,
                                                       d_info, props)
            self.assertIn('boot_mode', str(exc))

    @mock.patch.object(service_utils, 'is_glance_image')
    def test_validate_glance_partition_image(self, is_glance_image_mock):
        is_glance_image_mock.return_value = True
        self._test_validate(props_expected=['kernel_id', 'ramdisk_id'])

    def test_validate_whole_disk_image(self):
        self.node.driver_internal_info = {'is_whole_disk_image': True}
        self.node.save()
        self._test_validate(props_expected=[])

    @mock.patch.object(service_utils, 'is_glance_image')
    def test_validate_non_glance_partition_image(self, is_glance_image_mock):
        is_glance_image_mock.return_value = False
        self._test_validate(props_expected=['kernel', 'ramdisk'])

    @mock.patch.object(ilo_deploy, '_reboot_into')
    @mock.patch.object(deploy_utils, 'get_single_nic_with_vif_port_id')
    @mock.patch.object(agent, 'build_agent_options')
    @mock.patch.object(iscsi_deploy, 'build_deploy_ramdisk_options')
    @mock.patch.object(manager_utils, 'node_power_action')
    @mock.patch.object(iscsi_deploy, 'check_image_size')
    @mock.patch.object(iscsi_deploy, 'cache_instance_image')
    def test_deploy(self, cache_instance_image_mock, check_image_size_mock,
                    node_power_action_mock, build_opts_mock,
                    agent_options_mock, get_nic_mock, reboot_into_mock):
        deploy_opts = {'a': 'b'}
        agent_options_mock.return_value = {
            'ipa-api-url': 'http://1.2.3.4:6385'}
        build_opts_mock.return_value = deploy_opts
        get_nic_mock.return_value = '12:34:56:78:90:ab'
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:

            task.node.driver_info['ilo_deploy_iso'] = 'deploy-iso'
            returned_state = task.driver.deploy.deploy(task)

            node_power_action_mock.assert_any_call(task, states.POWER_OFF)
            cache_instance_image_mock.assert_called_once_with(task.context,
                    task.node)
            check_image_size_mock.assert_called_once_with(task)
            expected_ramdisk_opts = {'a': 'b', 'BOOTIF': '12:34:56:78:90:ab',
                                     'ipa-api-url': 'http://1.2.3.4:6385'}
            build_opts_mock.assert_called_once_with(task.node)
            get_nic_mock.assert_called_once_with(task)
            reboot_into_mock.assert_called_once_with(task, 'deploy-iso',
                                                     expected_ramdisk_opts)

        self.assertEqual(states.DEPLOYWAIT, returned_state)

    @mock.patch.object(manager_utils, 'node_power_action')
    def test_tear_down(self, node_power_action_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            returned_state = task.driver.deploy.tear_down(task)
            node_power_action_mock.assert_called_once_with(task,
                    states.POWER_OFF)
            self.assertEqual(states.DELETED, returned_state)

    @mock.patch.object(ilo_deploy, '_clean_up_boot_iso_for_instance')
    @mock.patch.object(iscsi_deploy, 'destroy_images')
    def test_clean_up(self, destroy_images_mock, clean_up_boot_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.deploy.clean_up(task)
            destroy_images_mock.assert_called_once_with(task.node.uuid)
            clean_up_boot_mock.assert_called_once_with(task.node)

    @mock.patch.object(ilo_common, 'update_boot_mode')
    def test_prepare(self,
                     update_boot_mode_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.deploy.prepare(task)
            update_boot_mode_mock.assert_called_once_with(task)


class IloVirtualMediaAgentDeployTestCase(db_base.DbTestCase):

    def setUp(self):
        super(IloVirtualMediaAgentDeployTestCase, self).setUp()
        mgr_utils.mock_the_extension_manager(driver="agent_ilo")
        self.node = obj_utils.create_test_node(self.context,
                driver='agent_ilo', driver_info=INFO_DICT)

    @mock.patch.object(ilo_deploy, '_parse_driver_info')
    def test_validate(self, parse_driver_info_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.deploy.validate(task)
            parse_driver_info_mock.assert_called_once_with(task.node)

    @mock.patch.object(ilo_deploy, '_prepare_agent_vmedia_boot')
    def test_deploy(self, vmedia_boot_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            returned_state = task.driver.deploy.deploy(task)
            vmedia_boot_mock.assert_called_once_with(task)
            self.assertEqual(states.DEPLOYWAIT, returned_state)

    @mock.patch.object(manager_utils, 'node_power_action')
    def test_tear_down(self, node_power_action_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            returned_state = task.driver.deploy.tear_down(task)
            node_power_action_mock.assert_called_once_with(task,
                    states.POWER_OFF)
            self.assertEqual(states.DELETED, returned_state)

    @mock.patch.object(ilo_common, 'update_boot_mode')
    @mock.patch.object(agent, 'build_instance_info_for_deploy')
    def test_prepare(self,
                     build_instance_info_mock,
                     update_boot_mode_mock):
        deploy_opts = {'a': 'b'}
        build_instance_info_mock.return_value = deploy_opts
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.deploy.prepare(task)
            update_boot_mode_mock.assert_called_once_with(task)
            self.assertEqual(deploy_opts, task.node.instance_info)

    @mock.patch('ironic.dhcp.neutron.NeutronDHCPApi.delete_cleaning_ports')
    @mock.patch('ironic.dhcp.neutron.NeutronDHCPApi.create_cleaning_ports')
    @mock.patch.object(ilo_deploy, '_prepare_agent_vmedia_boot')
    def test_prepare_cleaning(self, vmedia_boot_mock, create_port_mock,
                              delete_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            returned_state = task.driver.deploy.prepare_cleaning(task)
            vmedia_boot_mock.assert_called_once_with(task)
            self.assertEqual(states.CLEANING, returned_state)
            create_port_mock.assert_called_once_with(task)
            delete_mock.assert_called_once_with(task)

    @mock.patch('ironic.dhcp.neutron.NeutronDHCPApi.delete_cleaning_ports')
    @mock.patch.object(manager_utils, 'node_power_action')
    def test_tear_down_cleaning(self, power_mock, delete_mock):
        with task_manager.acquire(
                self.context, self.node['uuid'], shared=False) as task:
            task.driver.deploy.tear_down_cleaning(task)
            power_mock.assert_called_once_with(task, states.POWER_OFF)
            delete_mock.assert_called_once_with(task)

    @mock.patch.object(deploy_utils, 'agent_execute_clean_step')
    def test_execute_clean_step(self, execute_mock):
        with task_manager.acquire(
                self.context, self.node['uuid'], shared=False) as task:
            task.driver.deploy.execute_clean_step(task, 'fake-step')
            execute_mock.assert_called_once_with(task, 'fake-step')

    @mock.patch.object(deploy_utils, 'agent_get_clean_steps')
    def test_get_clean_steps_with_conf_option(self, get_clean_step_mock):
        self.config(clean_priority_erase_devices=20, group='ilo')
        get_clean_step_mock.return_value = [{
                'step': 'erase_devices',
                'priority': 10,
                'interface': 'deploy',
                'reboot_requested': False
                }]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            step = task.driver.deploy.get_clean_steps(task)
            get_clean_step_mock.assert_called_once_with(task)
            self.assertEqual(step[0].get('priority'),
                             CONF.ilo.clean_priority_erase_devices)

    @mock.patch.object(deploy_utils, 'agent_get_clean_steps')
    def test_get_clean_steps_without_conf_option(self, get_clean_step_mock):
        get_clean_step_mock.return_value = [{
                'step': 'erase_devices',
                'priority': 10,
                'interface': 'deploy',
                'reboot_requested': False
                }]
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            step = task.driver.deploy.get_clean_steps(task)
            get_clean_step_mock.assert_called_once_with(task)
            self.assertEqual(step[0].get('priority'), 10)


class VendorPassthruTestCase(db_base.DbTestCase):

    def setUp(self):
        super(VendorPassthruTestCase, self).setUp()
        mgr_utils.mock_the_extension_manager(driver="iscsi_ilo")
        self.node = obj_utils.create_test_node(self.context,
                                               driver='iscsi_ilo',
                                               driver_info=INFO_DICT)

    @mock.patch.object(iscsi_deploy, 'get_deploy_info')
    def test_validate_pass_deploy_info(self, get_deploy_info_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            vendor = ilo_deploy.VendorPassthru()
            vendor.validate(task, method='pass_deploy_info', foo='bar')
            get_deploy_info_mock.assert_called_once_with(task.node,
                                                         foo='bar')

    @mock.patch.object(iscsi_deploy, 'get_deploy_info')
    def test_validate_heartbeat(self, get_deploy_info_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            vendor = ilo_deploy.VendorPassthru()
            vendor.validate(task, method='heartbeat', foo='bar')
            self.assertFalse(get_deploy_info_mock.called)

    @mock.patch.object(deploy_utils, 'notify_deploy_complete')
    @mock.patch.object(manager_utils, 'node_set_boot_device')
    @mock.patch.object(ilo_common, 'setup_vmedia_for_boot')
    @mock.patch.object(ilo_deploy, '_get_boot_iso')
    @mock.patch.object(iscsi_deploy, 'continue_deploy')
    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    def test_pass_deploy_info_resume(self, cleanup_vmedia_boot_mock,
                                     continue_deploy_mock, get_boot_iso_mock,
                                     setup_vmedia_mock, set_boot_device_mock,
                                     notify_deploy_complete_mock):
        kwargs = {'method': 'pass_deploy_info', 'address': '123456'}
        continue_deploy_mock.return_value = {}
        get_boot_iso_mock.return_value = 'boot-iso'

        self.node.provision_state = states.DEPLOYWAIT
        self.node.target_provision_state = states.ACTIVE
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            vendor = ilo_deploy.VendorPassthru()
            vendor.pass_deploy_info(task, **kwargs)

            cleanup_vmedia_boot_mock.assert_called_once_with(task)
            continue_deploy_mock.assert_called_once_with(task, **kwargs)
            self.assertFalse(get_boot_iso_mock.called)
            self.assertFalse(setup_vmedia_mock.called)
            self.assertFalse(set_boot_device_mock.called)
            self.assertEqual(states.DEPLOYING, task.node.provision_state)
            self.assertEqual(states.ACTIVE,
                             task.node.target_provision_state)
        self.assertFalse(notify_deploy_complete_mock.called)

    @mock.patch.object(deploy_utils, 'notify_deploy_complete')
    @mock.patch.object(manager_utils, 'node_set_boot_device')
    @mock.patch.object(ilo_common, 'setup_vmedia_for_boot')
    @mock.patch.object(ilo_deploy, '_get_boot_iso')
    @mock.patch.object(iscsi_deploy, 'continue_deploy')
    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    def test_pass_deploy_info_good(self, cleanup_vmedia_boot_mock,
                                   continue_deploy_mock, get_boot_iso_mock,
                                   setup_vmedia_mock, set_boot_device_mock,
                                   notify_deploy_complete_mock):
        kwargs = {'method': 'pass_deploy_info', 'address': '123456'}
        continue_deploy_mock.return_value = {'root uuid': 'root-uuid'}
        get_boot_iso_mock.return_value = 'boot-iso'

        self.node.provision_state = states.DEPLOYWAIT
        self.node.target_provision_state = states.ACTIVE
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.vendor.pass_deploy_info(task, **kwargs)

            cleanup_vmedia_boot_mock.assert_called_once_with(task)
            continue_deploy_mock.assert_called_once_with(task, **kwargs)
            get_boot_iso_mock.assert_called_once_with(task, 'root-uuid')
            setup_vmedia_mock.assert_called_once_with(task, 'boot-iso')
            self.assertEqual(states.ACTIVE, task.node.provision_state)
            self.assertEqual(states.NOSTATE, task.node.target_provision_state)
            set_boot_device_mock.assert_called_once_with(task,
                                                         boot_devices.CDROM)
            self.assertEqual('boot-iso',
                             task.node.instance_info['ilo_boot_iso'])
            self.assertEqual(states.ACTIVE, task.node.provision_state)
            self.assertEqual(states.NOSTATE, task.node.target_provision_state)
        notify_deploy_complete_mock.assert_called_once_with('123456')

    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    def test_pass_deploy_info_bad(self, cleanup_vmedia_boot_mock):
        kwargs = {'method': 'pass_deploy_info', 'address': '123456'}

        self.node.provision_state = states.AVAILABLE
        self.node.target_provision_state = states.NOSTATE
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            vendor = task.driver.vendor
            self.assertRaises(exception.InvalidState,
                              vendor.pass_deploy_info,
                              task, **kwargs)
            self.assertEqual(states.AVAILABLE, task.node.provision_state)
            self.assertEqual(states.NOSTATE, task.node.target_provision_state)
        self.assertFalse(cleanup_vmedia_boot_mock.called)

    @mock.patch.object(iscsi_deploy, 'continue_deploy')
    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    def test_pass_deploy_info_no_root_uuid(self,
            cleanup_vmedia_boot_mock, continue_deploy_mock):
        kwargs = {'address': '123456'}
        continue_deploy_mock.return_value = {}

        self.node.provision_state = states.DEPLOYWAIT
        self.node.target_provision_state = states.ACTIVE
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.vendor.pass_deploy_info(task, **kwargs)

            cleanup_vmedia_boot_mock.assert_called_once_with(task)
            self.assertEqual(states.DEPLOYING, task.node.provision_state)
            self.assertEqual(states.ACTIVE, task.node.target_provision_state)
            continue_deploy_mock.assert_called_once_with(task, **kwargs)

    @mock.patch.object(manager_utils, 'node_power_action')
    @mock.patch.object(iscsi_deploy, 'continue_deploy')
    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    @mock.patch.object(ilo_deploy, '_get_boot_iso')
    def test_pass_deploy_info_create_boot_iso_fail(self, get_iso_mock,
            cleanup_vmedia_boot_mock, continue_deploy_mock, node_power_mock):
        kwargs = {'address': '123456'}
        continue_deploy_mock.return_value = {'root uuid': 'root-uuid'}
        get_iso_mock.side_effect = exception.ImageCreationFailed(
                                             image_type='iso', error="error")
        self.node.provision_state = states.DEPLOYWAIT
        self.node.target_provision_state = states.ACTIVE
        self.node.save()

        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.vendor.pass_deploy_info(task, **kwargs)

            cleanup_vmedia_boot_mock.assert_called_once_with(task)
            continue_deploy_mock.assert_called_once_with(task, **kwargs)
            get_iso_mock.assert_called_once_with(task, 'root-uuid')
            node_power_mock.assert_called_once_with(task, states.POWER_OFF)
            self.assertEqual(states.DEPLOYFAIL, task.node.provision_state)
            self.assertEqual(states.ACTIVE, task.node.target_provision_state)
            self.assertIsNotNone(task.node.last_error)

    @mock.patch.object(deploy_utils, 'notify_deploy_complete')
    @mock.patch.object(manager_utils, 'node_set_boot_device')
    @mock.patch.object(iscsi_deploy, 'continue_deploy')
    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    def _test_pass_deploy_info_localboot(self, cleanup_vmedia_boot_mock,
                                         continue_deploy_mock,
                                         set_boot_device_mock,
                                         notify_deploy_complete_mock):

        kwargs = {'method': 'pass_deploy_info', 'address': '123456'}
        continue_deploy_mock.return_value = {'root uuid': '<some-uuid>'}

        self.node.provision_state = states.DEPLOYWAIT
        self.node.target_provision_state = states.ACTIVE
        self.node.save()
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            vendor = task.driver.vendor
            vendor.pass_deploy_info(task, **kwargs)

            cleanup_vmedia_boot_mock.assert_called_once_with(task)
            continue_deploy_mock.assert_called_once_with(task, **kwargs)
            set_boot_device_mock.assert_called_once_with(task,
                                                         boot_devices.DISK,
                                                         persistent=True)
            notify_deploy_complete_mock.assert_called_once_with('123456')
            self.assertEqual(states.ACTIVE, task.node.provision_state)
            self.assertEqual(states.NOSTATE, task.node.target_provision_state)

    def test_pass_deploy_info_boot_option_local(self):
        self.node.instance_info = {'capabilities': '{"boot_option": "local"}'}
        self.node.save()
        self._test_pass_deploy_info_localboot()

    def test_pass_deploy_info_whole_disk_image(self):
        self.node.driver_internal_info = {'is_whole_disk_image': True}
        self.node.save()
        self._test_pass_deploy_info_localboot()

    @mock.patch.object(keystone, 'get_admin_auth_token')
    @mock.patch.object(agent_base_vendor.BaseAgentVendor,
                       'reboot_and_finish_deploy')
    @mock.patch.object(ilo_deploy.VendorPassthru, '_configure_vmedia_boot')
    @mock.patch.object(iscsi_deploy, 'do_agent_iscsi_deploy')
    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    def test_continue_deploy_netboot(self, cleanup_vmedia_boot_mock,
                                     do_agent_iscsi_deploy_mock,
                                     configure_vmedia_boot_mock,
                                     reboot_and_finish_deploy_mock,
                                     keystone_mock):
        self.node.provision_state = states.DEPLOYWAIT
        self.node.target_provision_state = states.DEPLOYING
        self.node.save()
        do_agent_iscsi_deploy_mock.return_value = {
            'root uuid': 'some-root-uuid'}
        keystone_mock.return_value = 'admin-token'
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.vendor.continue_deploy(task)
            cleanup_vmedia_boot_mock.assert_called_once_with(task)
            do_agent_iscsi_deploy_mock.assert_called_once_with(task,
                                                               mock.ANY)
            configure_vmedia_boot_mock.assert_called_once_with(
                task, 'some-root-uuid')
            reboot_and_finish_deploy_mock.assert_called_once_with(task)
            # Ensure that admin token is populated in task
            self.assertEqual('admin-token', task.context.auth_token)

    @mock.patch.object(agent_base_vendor.BaseAgentVendor,
                       'reboot_and_finish_deploy')
    @mock.patch.object(agent_base_vendor.BaseAgentVendor,
                       'configure_local_boot')
    @mock.patch.object(iscsi_deploy, 'do_agent_iscsi_deploy')
    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    def test_continue_deploy_localboot(self, cleanup_vmedia_boot_mock,
                                       do_agent_iscsi_deploy_mock,
                                       configure_local_boot_mock,
                                       reboot_and_finish_deploy_mock):
        self.node.provision_state = states.DEPLOYWAIT
        self.node.target_provision_state = states.DEPLOYING
        self.node.instance_info = {
            'capabilities': {'boot_option': 'local'}}
        self.node.save()
        do_agent_iscsi_deploy_mock.return_value = {
            'root uuid': 'some-root-uuid'}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.vendor.continue_deploy(task)
            cleanup_vmedia_boot_mock.assert_called_once_with(task)
            do_agent_iscsi_deploy_mock.assert_called_once_with(task,
                                                               mock.ANY)
            configure_local_boot_mock.assert_called_once_with(
                task, root_uuid='some-root-uuid',
                efi_system_part_uuid=None)
            reboot_and_finish_deploy_mock.assert_called_once_with(task)

    @mock.patch.object(agent_base_vendor.BaseAgentVendor,
                       'reboot_and_finish_deploy')
    @mock.patch.object(agent_base_vendor.BaseAgentVendor,
                       'configure_local_boot')
    @mock.patch.object(iscsi_deploy, 'do_agent_iscsi_deploy')
    @mock.patch.object(ilo_common, 'cleanup_vmedia_boot')
    def test_continue_deploy_localboot_uefi(self, cleanup_vmedia_boot_mock,
                                            do_agent_iscsi_deploy_mock,
                                            configure_local_boot_mock,
                                            reboot_and_finish_deploy_mock):
        self.node.provision_state = states.DEPLOYWAIT
        self.node.target_provision_state = states.DEPLOYING
        self.node.instance_info = {
            'capabilities': {'boot_option': 'local'}}
        self.node.save()
        do_agent_iscsi_deploy_mock.return_value = {
            'root uuid': 'some-root-uuid',
            'efi system partition uuid': 'efi-system-part-uuid'}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.vendor.continue_deploy(task)
            cleanup_vmedia_boot_mock.assert_called_once_with(task)
            do_agent_iscsi_deploy_mock.assert_called_once_with(task,
                                                               mock.ANY)
            configure_local_boot_mock.assert_called_once_with(
                task, root_uuid='some-root-uuid',
                efi_system_part_uuid='efi-system-part-uuid')
            reboot_and_finish_deploy_mock.assert_called_once_with(task)


class IloPXEDeployTestCase(db_base.DbTestCase):

    def setUp(self):
        super(IloPXEDeployTestCase, self).setUp()
        mgr_utils.mock_the_extension_manager(driver="pxe_ilo")
        self.node = obj_utils.create_test_node(self.context,
                driver='pxe_ilo', driver_info=INFO_DICT)

    @mock.patch.object(pxe.PXEDeploy, 'validate')
    def test_validate(self, pxe_validate_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.deploy.validate(task)
            pxe_validate_mock.assert_called_once_with(task)

    @mock.patch.object(pxe.PXEDeploy, 'prepare')
    @mock.patch.object(ilo_common, 'update_boot_mode')
    def test_prepare(self,
                     update_boot_mode_mock,
                     pxe_prepare_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.deploy.prepare(task)
            update_boot_mode_mock.assert_called_once_with(task)
            pxe_prepare_mock.assert_called_once_with(task)

    @mock.patch.object(pxe.PXEDeploy, 'deploy')
    @mock.patch.object(manager_utils, 'node_set_boot_device')
    def test_deploy_boot_mode_exists(self, set_persistent_mock,
                                     pxe_deploy_mock):
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.driver.deploy.deploy(task)
            set_persistent_mock.assert_called_with(task, boot_devices.PXE)
            pxe_deploy_mock.assert_called_once_with(task)


class IloPXEVendorPassthruTestCase(db_base.DbTestCase):

    def setUp(self):
        super(IloPXEVendorPassthruTestCase, self).setUp()
        mgr_utils.mock_the_extension_manager(driver="pxe_ilo")
        self.node = obj_utils.create_test_node(self.context,
                driver='pxe_ilo', driver_info=INFO_DICT)

    def test_vendor_routes(self):
        expected = ['heartbeat', 'pass_deploy_info']
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            vendor_routes = task.driver.vendor.vendor_routes
            self.assertIsInstance(vendor_routes, dict)
            self.assertEqual(sorted(expected), sorted(list(vendor_routes)))

    def test_driver_routes(self):
        expected = ['lookup']
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=True) as task:
            driver_routes = task.driver.vendor.driver_routes
            self.assertIsInstance(driver_routes, dict)
            self.assertEqual(sorted(expected), sorted(list(driver_routes)))

    @mock.patch.object(pxe.VendorPassthru, 'pass_deploy_info')
    @mock.patch.object(manager_utils, 'node_set_boot_device')
    def test_vendorpassthru_pass_deploy_info(self, set_boot_device_mock,
                                             pxe_vendorpassthru_mock):
        kwargs = {'address': '123456'}
        with task_manager.acquire(self.context, self.node.uuid,
                                  shared=False) as task:
            task.node.provision_state = states.DEPLOYWAIT
            task.node.target_provision_state = states.ACTIVE
            task.driver.vendor.pass_deploy_info(task, **kwargs)
            set_boot_device_mock.assert_called_with(task, boot_devices.PXE,
                                                    True)
            pxe_vendorpassthru_mock.assert_called_once_with(task, **kwargs)
