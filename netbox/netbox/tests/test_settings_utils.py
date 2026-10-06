import os
import sys
import tempfile
from types import ModuleType
from unittest.mock import patch

from django.conf import settings as django_settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from rq.queue import Queue

from netbox import settings_utils


class LoadConfigurationTest(SimpleTestCase):
    def test_explicit_module_wins(self):
        with patch('netbox.settings_utils.importlib.import_module') as import_module:
            settings_utils.load_configuration(
                install_mode='wheel', install_root='/opt/netbox',
                environ={'NETBOX_CONFIGURATION': 'my.config'},
            )
        import_module.assert_called_once_with('my.config')

    def test_checkout_uses_default_module(self):
        with patch('netbox.settings_utils.importlib.import_module') as import_module:
            settings_utils.load_configuration(
                install_mode='checkout', install_root='/repo', environ={},
            )
        import_module.assert_called_once_with('netbox.configuration')

    def test_checkout_missing_module_raises_improperly_configured(self):
        with patch(
            'netbox.settings_utils.importlib.import_module',
            side_effect=ModuleNotFoundError("No module named 'netbox.configuration'", name='netbox.configuration'),
        ):
            with self.assertRaises(ImproperlyConfigured):
                settings_utils.load_configuration(
                    install_mode='checkout', install_root='/repo', environ={},
                )

    def test_wheel_prefers_conf_dir(self):
        with tempfile.TemporaryDirectory() as root:
            conf = os.path.join(root, 'conf')
            os.mkdir(conf)
            preferred = os.path.join(conf, 'configuration.py')
            open(preferred, 'w').close()
            saved = list(sys.path)
            try:
                with patch('netbox.settings_utils._import_from_path') as import_from_path:
                    settings_utils.load_configuration(
                        install_mode='wheel', install_root=root, environ={},
                    )
                import_from_path.assert_called_once_with('netbox_local_configuration', preferred)
                self.assertEqual(sys.path, saved)
            finally:
                sys.path[:] = saved

    def test_wheel_falls_back_to_legacy_with_warning(self):
        with tempfile.TemporaryDirectory() as root:
            legacy_dir = os.path.join(root, 'netbox', 'netbox')
            os.makedirs(legacy_dir)
            legacy = os.path.join(legacy_dir, 'configuration.py')
            open(legacy, 'w').close()
            with (
                patch('netbox.settings_utils._import_from_path') as importer,
                self.assertWarns(RuntimeWarning),
            ):
                settings_utils.load_configuration(
                    install_mode='wheel', install_root=root, environ={},
                )
            self.assertEqual(importer.call_args.args[1], legacy)

    def test_wheel_missing_configuration_raises(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesMessage(ImproperlyConfigured, 'conf/configuration.py'):
                settings_utils.load_configuration(
                    install_mode='wheel', install_root=root, environ={},
                )

    def test_explicit_module_reraises_other_import_error(self):
        # A missing dependency of the config module must propagate, not become a friendly error.
        with patch(
            'netbox.settings_utils.importlib.import_module',
            side_effect=ModuleNotFoundError("No module named 'missing_dep'", name='missing_dep'),
        ):
            with self.assertRaises(ModuleNotFoundError):
                settings_utils.load_configuration(
                    install_mode='checkout', install_root='/repo',
                    environ={'NETBOX_CONFIGURATION': 'my.config'},
                )

    def test_import_from_path_loads_module_and_restores_sys_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'legacy_cfg.py')
            with open(path, 'w') as handle:
                handle.write('ALLOWED_HOSTS = ["example"]\n')
            self.addCleanup(sys.modules.pop, 'netbox_test_legacy_cfg', None)
            saved = list(sys.path)
            module = settings_utils._import_from_path('netbox_test_legacy_cfg', path)
            self.assertEqual(module.ALLOWED_HOSTS, ['example'])
            self.assertEqual(sys.path, saved)
            self.assertIs(sys.modules['netbox_test_legacy_cfg'], module)

    def test_import_from_path_removes_module_on_failure(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'broken_cfg.py')
            with open(path, 'w') as handle:
                handle.write('raise RuntimeError("Simulated configuration error")\n')
            with self.assertRaisesMessage(RuntimeError, 'Simulated configuration error'):
                settings_utils._import_from_path('netbox_test_broken_cfg', path)
            self.assertNotIn('netbox_test_broken_cfg', sys.modules)

    def test_import_from_path_rejects_unloadable_path(self):
        # A suffix-less file yields no loader; the helper must fail cleanly.
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'noext')
            open(path, 'w').close()
            with self.assertRaisesMessage(ImproperlyConfigured, 'Unable to load'):
                settings_utils._import_from_path('netbox_test_noext_cfg', path)

    def test_import_from_path_preserves_preexisting_sys_path_entry(self):
        # Only the index-0 entry this helper inserted is popped; a pre-existing entry survives.
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, 'preexisting_cfg.py')
            with open(path, 'w') as handle:
                handle.write('ALLOWED_HOSTS = ["example"]\n')
            self.addCleanup(sys.modules.pop, 'netbox_test_preexisting_cfg', None)
            saved = list(sys.path)
            sys.path.append(root)
            try:
                settings_utils._import_from_path('netbox_test_preexisting_cfg', path)
                self.assertEqual(sys.path, saved + [root])
            finally:
                sys.path[:] = saved

    def test_import_from_path_reuses_module_loaded_from_same_path(self):
        """A repeated load of the same path returns the first module and runs the file only once."""
        with tempfile.TemporaryDirectory() as root:
            marker = os.path.join(root, 'executions')
            path = os.path.join(root, 'cached_cfg.py')
            with open(path, 'w') as handle:
                handle.write(f'with open({marker!r}, "a") as handle:\n    handle.write("x")\n')
            self.addCleanup(sys.modules.pop, 'netbox_test_cached_cfg', None)
            first = settings_utils._import_from_path('netbox_test_cached_cfg', path)
            second = settings_utils._import_from_path('netbox_test_cached_cfg', path)
            self.assertIs(second, first)
            with open(marker) as handle:
                self.assertEqual(handle.read(), 'x')

    def test_import_from_path_replaces_module_loaded_from_another_path(self):
        """The same module name at a different path is loaded fresh, not served from the cache."""
        with tempfile.TemporaryDirectory() as root:
            first_path = os.path.join(root, 'first_cfg.py')
            second_path = os.path.join(root, 'second_cfg.py')
            with open(first_path, 'w') as handle:
                handle.write('ALLOWED_HOSTS = ["first"]\n')
            with open(second_path, 'w') as handle:
                handle.write('ALLOWED_HOSTS = ["second"]\n')
            self.addCleanup(sys.modules.pop, 'netbox_test_switched_cfg', None)
            settings_utils._import_from_path('netbox_test_switched_cfg', first_path)
            module = settings_utils._import_from_path('netbox_test_switched_cfg', second_path)
            self.assertEqual(module.ALLOWED_HOSTS, ['second'])
            self.assertIs(sys.modules['netbox_test_switched_cfg'], module)

    def test_import_from_path_restores_previous_module_on_failure(self):
        """A failed replacement does not evict the previously loaded module."""
        with tempfile.TemporaryDirectory() as root:
            module_name = 'netbox_test_restore_cfg'
            first_path = os.path.join(root, 'first_cfg.py')
            broken_path = os.path.join(root, 'broken_cfg.py')
            with open(first_path, 'w') as handle:
                handle.write('ALLOWED_HOSTS = ["first"]\n')
            with open(broken_path, 'w') as handle:
                handle.write('raise RuntimeError("Simulated configuration error")\n')
            self.addCleanup(sys.modules.pop, module_name, None)
            first = settings_utils._import_from_path(module_name, first_path)
            with self.assertRaisesMessage(RuntimeError, 'Simulated configuration error'):
                settings_utils._import_from_path(module_name, broken_path)
            self.assertIs(sys.modules[module_name], first)
            self.assertIs(settings_utils._import_from_path(module_name, first_path), first)

    def test_wheel_both_configs_present_warns_and_prefers_conf(self):
        with tempfile.TemporaryDirectory() as root:
            conf = os.path.join(root, 'conf')
            os.mkdir(conf)
            preferred = os.path.join(conf, 'configuration.py')
            open(preferred, 'w').close()
            legacy_dir = os.path.join(root, 'netbox', 'netbox')
            os.makedirs(legacy_dir)
            open(os.path.join(legacy_dir, 'configuration.py'), 'w').close()
            saved = list(sys.path)
            try:
                with (
                    patch('netbox.settings_utils._import_from_path') as import_from_path,
                    self.assertWarns(RuntimeWarning),
                ):
                    settings_utils.load_configuration(install_mode='wheel', install_root=root, environ={})
                import_from_path.assert_called_once_with('netbox_local_configuration', preferred)
            finally:
                sys.path[:] = saved


class ConfigurationDirTest(SimpleTestCase):
    def test_returns_directory_of_module_file(self):
        module = ModuleType('cfg')
        module.__file__ = '/srv/netbox/conf/configuration.py'
        self.assertEqual(settings_utils.get_configuration_dir(module), '/srv/netbox/conf')

    def test_returns_none_without_file(self):
        self.assertIsNone(settings_utils.get_configuration_dir(ModuleType('cfg')))


class ResolveInstallPathsTest(SimpleTestCase):
    """resolve_install_paths() centralizes wheel-vs-checkout filesystem layout decisions."""

    def test_checkout_roots(self):
        with tempfile.TemporaryDirectory() as root:
            settings_dir = os.path.join(root, 'netbox', 'netbox')
            os.makedirs(settings_dir)
            base_dir = os.path.join(root, 'netbox')
            paths = settings_utils.resolve_install_paths(settings_dir, {})
            self.assertEqual(paths.install_mode, 'checkout')
            self.assertEqual(paths.base_dir, base_dir)
            self.assertEqual(paths.netbox_root, base_dir)
            self.assertEqual(paths.docs_root, os.path.join(root, 'docs'))
            self.assertEqual(paths.static_docs_root, os.path.join(base_dir, 'project-static', 'docs'))

    def test_wheel_roots_default_netbox_root(self):
        with tempfile.TemporaryDirectory() as root:
            settings_dir = os.path.join(root, 'site-packages', 'netbox')
            base_dir = os.path.join(settings_dir, '_data')
            os.makedirs(base_dir)
            paths = settings_utils.resolve_install_paths(settings_dir, {})
            self.assertEqual(paths.install_mode, 'wheel')
            self.assertEqual(paths.base_dir, base_dir)
            self.assertEqual(paths.netbox_root, '/opt/netbox')
            self.assertEqual(paths.docs_root, os.path.join(base_dir, 'docs'))
            self.assertEqual(paths.static_docs_root, os.path.join(base_dir, 'docs'))

    def test_netbox_root_env_override_is_abspathed(self):
        with tempfile.TemporaryDirectory() as root:
            settings_dir = os.path.join(root, 'site-packages', 'netbox')
            os.makedirs(os.path.join(settings_dir, '_data'))
            paths = settings_utils.resolve_install_paths(settings_dir, {'NETBOX_ROOT': 'relative/root'})
            self.assertEqual(paths.netbox_root, os.path.abspath('relative/root'))


class SecretKeyHintTest(SimpleTestCase):
    """secret_key_hint() picks the SECRET_KEY-too-short hint by install mode."""

    def test_wheel_mode_suggests_console_command(self):
        self.assertEqual(settings_utils.secret_key_hint('wheel', '/opt/netbox/lib/netbox'), 'netbox secret-key')

    def test_checkout_mode_suggests_generate_secret_key_script(self):
        self.assertEqual(
            settings_utils.secret_key_hint('checkout', '/repo/netbox'),
            'python /repo/netbox/generate_secret_key.py',
        )


class ParseJobTimeoutTest(SimpleTestCase):
    """parse_job_timeout() normalizes RQ_DEFAULT_TIMEOUT to a comparable number of seconds."""

    def test_integer_is_returned_unchanged(self):
        self.assertEqual(settings_utils.parse_job_timeout(300), 300)

    def test_numeric_string_is_coerced(self):
        self.assertEqual(settings_utils.parse_job_timeout('300'), 300)

    def test_duration_string_is_normalized(self):
        self.assertEqual(settings_utils.parse_job_timeout('1h'), 3600)
        self.assertEqual(settings_utils.parse_job_timeout('30m'), 1800)
        self.assertEqual(settings_utils.parse_job_timeout('45s'), 45)

    def test_absent_or_zero_timeout_falls_back_to_queue_default(self):
        # RQ does not treat a null or zero default timeout as unlimited: Queue substitutes its own
        # default, which remains a real ceiling on job execution.
        for value in (None, 0, '0'):
            with self.subTest(value=value):
                self.assertEqual(settings_utils.parse_job_timeout(value), Queue.DEFAULT_TIMEOUT)

    def test_negative_timeout_is_unbounded(self):
        # -1 is RQ's documented infinite timeout; it disables the death penalty, so there is no
        # ceiling to compare against.
        self.assertIsNone(settings_utils.parse_job_timeout(-1))
        self.assertIsNone(settings_utils.parse_job_timeout('-1'))

    def test_invalid_value_raises(self):
        for value in ('1x', 'abc', [300]):
            with self.subTest(value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, 'RQ_DEFAULT_TIMEOUT'):
                    settings_utils.parse_job_timeout(value)


class ValidateWebhookDefaultTimeoutTest(SimpleTestCase):
    """validate_webhook_default_timeout() is the startup check applied to WEBHOOK_DEFAULT_TIMEOUT."""

    def test_valid_timeout_below_job_timeout(self):
        settings_utils.validate_webhook_default_timeout(60, 300)

    def test_timeout_at_or_above_job_timeout_raises(self):
        for timeout in (300, 301):
            with self.subTest(timeout=timeout):
                with self.assertRaisesMessage(ImproperlyConfigured, 'must be less than RQ_DEFAULT_TIMEOUT'):
                    settings_utils.validate_webhook_default_timeout(timeout, 300)

    def test_normalized_job_timeout_is_enforced(self):
        # A duration string such as "1h" must be normalized by the caller and enforced like any other value.
        job_timeout = settings_utils.parse_job_timeout('1h')
        with self.assertRaises(ImproperlyConfigured):
            settings_utils.validate_webhook_default_timeout(3600, job_timeout)
        settings_utils.validate_webhook_default_timeout(3599, job_timeout)

    def test_unbounded_job_timeout_skips_comparison(self):
        settings_utils.validate_webhook_default_timeout(3600, None)

    def test_out_of_range_timeout_raises(self):
        for timeout in (0, 3601):
            with self.subTest(timeout=timeout):
                with self.assertRaisesMessage(ImproperlyConfigured, 'between 1 and 3600'):
                    settings_utils.validate_webhook_default_timeout(timeout, None)

    def test_non_integer_timeout_raises(self):
        for timeout in ('60', 60.5, None):
            with self.subTest(timeout=timeout):
                with self.assertRaisesMessage(ImproperlyConfigured, 'must be an integer'):
                    settings_utils.validate_webhook_default_timeout(timeout, 300)


class LoadLdapConfigTest(SimpleTestCase):
    def test_loads_sibling_ldap_config(self):
        with tempfile.TemporaryDirectory() as conf_dir:
            with open(os.path.join(conf_dir, 'ldap_config.py'), 'w') as handle:
                handle.write('AUTH_LDAP_SERVER_URI = "ldaps://example"\n')
            self.addCleanup(sys.modules.pop, 'netbox.ldap_config', None)
            module = settings_utils.load_ldap_config(conf_dir)
            self.assertEqual(module.AUTH_LDAP_SERVER_URI, 'ldaps://example')
            self.assertIs(sys.modules['netbox.ldap_config'], module)

    def test_repeated_calls_reuse_the_sibling_module(self):
        """Two calls with an unchanged sibling ldap_config.py return the same module object."""
        with tempfile.TemporaryDirectory() as conf_dir:
            with open(os.path.join(conf_dir, 'ldap_config.py'), 'w') as handle:
                handle.write('AUTH_LDAP_SERVER_URI = "ldaps://example"\n')
            self.addCleanup(sys.modules.pop, 'netbox.ldap_config', None)
            first = settings_utils.load_ldap_config(conf_dir)
            second = settings_utils.load_ldap_config(conf_dir)
            self.assertIs(second, first)

    def test_legacy_fallback_loads_historical_module_with_warning(self):
        legacy = ModuleType('netbox.ldap_config')
        legacy.AUTH_LDAP_SERVER_URI = 'ldaps://legacy'
        with tempfile.TemporaryDirectory() as conf_dir:
            with patch.dict(sys.modules, {'netbox.ldap_config': legacy}), self.assertWarns(RuntimeWarning):
                module = settings_utils.load_ldap_config(conf_dir, allow_legacy_fallback=True)
        self.assertIs(module, legacy)

    def test_legacy_fallback_prefers_sibling_file(self):
        legacy = ModuleType('netbox.ldap_config')
        legacy.AUTH_LDAP_SERVER_URI = 'ldaps://legacy'
        with tempfile.TemporaryDirectory() as conf_dir:
            with open(os.path.join(conf_dir, 'ldap_config.py'), 'w') as handle:
                handle.write('AUTH_LDAP_SERVER_URI = "ldaps://sibling"\n')
            with patch.dict(sys.modules, {'netbox.ldap_config': legacy}):
                module = settings_utils.load_ldap_config(conf_dir, allow_legacy_fallback=True)
            self.assertEqual(module.AUTH_LDAP_SERVER_URI, 'ldaps://sibling')

    def test_legacy_fallback_disabled_raises(self):
        legacy = ModuleType('netbox.ldap_config')
        with tempfile.TemporaryDirectory() as conf_dir:
            with patch.dict(sys.modules, {'netbox.ldap_config': legacy}):
                with self.assertRaisesMessage(ImproperlyConfigured, 'alongside configuration.py'):
                    settings_utils.load_ldap_config(conf_dir)

    def test_legacy_fallback_missing_module_raises(self):
        with tempfile.TemporaryDirectory() as conf_dir:
            with patch(
                'netbox.settings_utils.importlib.import_module',
                side_effect=ModuleNotFoundError("No module named 'netbox.ldap_config'", name='netbox.ldap_config'),
            ):
                with self.assertRaisesMessage(ImproperlyConfigured, 'alongside configuration.py'):
                    settings_utils.load_ldap_config(conf_dir, allow_legacy_fallback=True)

    def test_legacy_fallback_reraises_broken_dependency(self):
        with tempfile.TemporaryDirectory() as conf_dir:
            with patch(
                'netbox.settings_utils.importlib.import_module',
                side_effect=ModuleNotFoundError("No module named 'missing_dep'", name='missing_dep'),
            ):
                with self.assertRaises(ModuleNotFoundError):
                    settings_utils.load_ldap_config(conf_dir, allow_legacy_fallback=True)

    def test_none_config_dir_raises(self):
        with self.assertRaisesMessage(ImproperlyConfigured, 'unable to determine'):
            settings_utils.load_ldap_config(None)

    def test_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as conf_dir:
            with self.assertRaisesMessage(ImproperlyConfigured, 'ldap_config.py'):
                settings_utils.load_ldap_config(conf_dir)

    def test_configuration_dir_setting_matches_active_configuration(self):
        from netbox import configuration_testing
        self.assertEqual(
            django_settings.CONFIGURATION_DIR,
            os.path.dirname(os.path.abspath(configuration_testing.__file__)),
        )


class UsesSentinelTest(SimpleTestCase):
    def test_non_empty_list_or_tuple(self):
        self.assertTrue(settings_utils.uses_sentinel({'SENTINELS': [('s1', 26379)]}))
        self.assertTrue(settings_utils.uses_sentinel({'SENTINELS': (('s1', 26379),)}))

    def test_absent_empty_or_wrong_type(self):
        self.assertFalse(settings_utils.uses_sentinel({}))
        self.assertFalse(settings_utils.uses_sentinel({'SENTINELS': []}))
        self.assertFalse(settings_utils.uses_sentinel({'SENTINELS': None}))
        self.assertFalse(settings_utils.uses_sentinel({'SENTINELS': 's1:26379'}))


class BuildRqParamsTest(SimpleTestCase):
    def test_host_branch_defaults(self):
        params = settings_utils.build_rq_params({}, 300)
        self.assertEqual(params, {
            'HOST': 'localhost',
            'PORT': 6379,
            'SSL': False,
            'SSL_CERT_REQS': 'required',
            'DB': 0,
            'USERNAME': '',
            'PASSWORD': '',
            'DEFAULT_TIMEOUT': 300,
        })

    def test_host_branch_is_unchanged(self):
        config = {
            'HOST': 'redis.example.com',
            'PORT': 6380,
            'USERNAME': 'netbox',
            'PASSWORD': 'p@ss',
            'DATABASE': 3,
            'SSL': True,
            'INSECURE_SKIP_TLS_VERIFY': True,
            'CA_CERT_PATH': '/ca.pem',
            'KWARGS': {'socket_timeout': 5, 'ssl_check_hostname': False},
        }
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params, {
            'HOST': 'redis.example.com',
            'PORT': 6380,
            'SSL': True,
            'SSL_CERT_REQS': None,
            'DB': 3,
            'USERNAME': 'netbox',
            'PASSWORD': 'p@ss',
            'DEFAULT_TIMEOUT': 300,
            'REDIS_CLIENT_KWARGS': {'ssl_ca_certs': '/ca.pem', 'socket_timeout': 5, 'ssl_check_hostname': False},
        })
        # Key order is preserved as well
        self.assertEqual(list(params), [
            'HOST', 'PORT', 'SSL', 'SSL_CERT_REQS', 'DB', 'USERNAME', 'PASSWORD', 'DEFAULT_TIMEOUT',
            'REDIS_CLIENT_KWARGS',
        ])

    def test_host_branch_kwargs_override_ca_cert(self):
        config = {'CA_CERT_PATH': '/ca.pem', 'KWARGS': {'ssl_ca_certs': '/other.pem'}}
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['REDIS_CLIENT_KWARGS'], {'ssl_ca_certs': '/other.pem'})

    def test_host_branch_does_not_mutate_config(self):
        kwargs = {'socket_timeout': 5}
        config = {'CA_CERT_PATH': '/ca.pem', 'KWARGS': kwargs}
        params = settings_utils.build_rq_params(config, 300)
        params['REDIS_CLIENT_KWARGS']['extra'] = True
        self.assertEqual(kwargs, {'socket_timeout': 5})

    def test_sentinel_branch(self):
        config = {
            'SENTINELS': [('s1', 26379), ('s2', 26379)],
            'SENTINEL_SERVICE': 'mymaster',
            'SENTINEL_TIMEOUT': 3,
            'USERNAME': 'netbox',
            'PASSWORD': 'secret',
            'DATABASE': 1,
        }
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params, {
            'SENTINELS': [('s1', 26379), ('s2', 26379)],
            'MASTER_NAME': 'mymaster',
            'SOCKET_TIMEOUT': None,
            'CONNECTION_KWARGS': {'socket_connect_timeout': 3},
            'SENTINEL_KWARGS': {'socket_connect_timeout': 3},
            'DB': 1,
            'USERNAME': 'netbox',
            'PASSWORD': 'secret',
            'DEFAULT_TIMEOUT': 300,
        })

    def test_sentinel_credentials(self):
        config = {
            'SENTINELS': [('s1', 26379)],
            'USERNAME': 'netbox',
            'PASSWORD': 'secret',
            'SENTINEL_USERNAME': 'sentinel-user',
            'SENTINEL_PASSWORD': 'sentinel-secret',
        }
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['SENTINEL_KWARGS'], {
            'socket_connect_timeout': 10,
            'username': 'sentinel-user',
            'password': 'sentinel-secret',
        })
        # Data-node credentials are unchanged
        self.assertEqual(params['USERNAME'], 'netbox')
        self.assertEqual(params['PASSWORD'], 'secret')

    def test_sentinel_connection_wiring(self):
        # redis-py connects lazily, so this inspects the client django-rq builds without any network I/O.
        from django_rq.connection_utils import get_redis_connection

        config = {
            'SENTINELS': [('s1', 26379)],
            'SENTINEL_SERVICE': 'mymaster',
            'SENTINEL_TIMEOUT': 4,
            'PASSWORD': 'secret',
            'SENTINEL_PASSWORD': 'sentinel-secret',
        }
        connection = get_redis_connection(settings_utils.build_rq_params(config, 300))
        pool = connection.connection_pool
        self.assertEqual(pool.service_name, 'mymaster')
        self.assertEqual(pool.connection_kwargs['password'], 'secret')
        self.assertEqual(pool.connection_kwargs['socket_connect_timeout'], 4)
        sentinel_node_kwargs = pool.sentinel_manager.sentinels[0].connection_pool.connection_kwargs
        self.assertEqual(sentinel_node_kwargs['password'], 'sentinel-secret')
        self.assertEqual(sentinel_node_kwargs['socket_connect_timeout'], 4)

    def test_sentinel_ssl_and_kwargs_land_in_connection_kwargs(self):
        config = {
            'SENTINELS': [('s1', 26379)],
            'SENTINEL_TIMEOUT': 3,
            'SSL': True,
            'INSECURE_SKIP_TLS_VERIFY': True,
            'CA_CERT_PATH': '/ca.pem',
            'KWARGS': {'socket_timeout': 5, 'ssl_check_hostname': False},
        }
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['CONNECTION_KWARGS'], {
            'socket_connect_timeout': 3,
            'ssl': True,
            'ssl_cert_reqs': None,
            'ssl_ca_certs': '/ca.pem',
            'socket_timeout': 5,
            'ssl_check_hostname': False,
        })
        # TLS options for the data node are not applied to the Sentinel nodes; socket options are
        self.assertEqual(params['SENTINEL_KWARGS'], {'socket_timeout': 5, 'socket_connect_timeout': 3})
        # REDIS_CLIENT_KWARGS is still emitted, although django-rq ignores it in Sentinel mode
        self.assertEqual(params['REDIS_CLIENT_KWARGS'], {
            'ssl_ca_certs': '/ca.pem', 'socket_timeout': 5, 'ssl_check_hostname': False,
        })

    def test_sentinel_ssl_verifies_certificates_by_default(self):
        config = {'SENTINELS': [('s1', 26379)], 'SSL': True}
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['CONNECTION_KWARGS'], {
            'socket_connect_timeout': 10,
            'ssl': True,
            'ssl_cert_reqs': 'required',
        })

    def test_sentinel_without_ssl_ignores_ca_cert_path(self):
        config = {'SENTINELS': [('s1', 26379)], 'CA_CERT_PATH': '/ca.pem'}
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['CONNECTION_KWARGS'], {'socket_connect_timeout': 10})

    def test_sentinel_ssl_connection_wiring(self):
        from django_rq.connection_utils import get_redis_connection
        from redis.sentinel import SentinelManagedSSLConnection

        config = {'SENTINELS': [('s1', 26379)], 'SSL': True, 'CA_CERT_PATH': '/ca.pem'}
        connection = get_redis_connection(settings_utils.build_rq_params(config, 300))
        pool = connection.connection_pool
        self.assertIs(pool.connection_class, SentinelManagedSSLConnection)
        self.assertEqual(pool.connection_kwargs['ssl_ca_certs'], '/ca.pem')
        self.assertEqual(pool.connection_kwargs['ssl_cert_reqs'], 'required')

    def test_sentinel_takes_precedence_over_url(self):
        config = {'SENTINELS': [('s1', 26379)], 'URL': 'redis://h/0'}
        params = settings_utils.build_rq_params(config, 300)
        self.assertIn('SENTINELS', params)
        self.assertNotIn('URL', params)

    def test_url_branch(self):
        config = {'URL': 'redis://h:6379/1', 'DATABASE': 2}
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params, {
            'URL': 'redis://h:6379/1',
            'SSL': False,
            'SSL_CERT_REQS': 'required',
            'DB': 2,
            'USERNAME': '',
            'PASSWORD': '',
            'DEFAULT_TIMEOUT': 300,
        })

    def test_url_branch_embeds_credentials(self):
        config = {'URL': 'redis://redis.example.com:6379/1', 'USERNAME': 'netbox', 'PASSWORD': 'secret'}
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['URL'], 'redis://netbox:secret@redis.example.com:6379/1')
        self.assertEqual(params['USERNAME'], 'netbox')
        self.assertEqual(params['PASSWORD'], 'secret')

    def test_url_branch_embeds_credentials_for_rediss_and_unix(self):
        config = {'URL': 'rediss://redis.example.com:6380/0', 'PASSWORD': 'secret'}
        self.assertEqual(
            settings_utils.build_rq_params(config, 300)['URL'], 'rediss://:secret@redis.example.com:6380/0'
        )
        config = {'URL': 'unix:///var/run/redis.sock?db=2', 'USERNAME': 'netbox', 'PASSWORD': 'secret'}
        self.assertEqual(
            settings_utils.build_rq_params(config, 300)['URL'], 'unix://netbox:secret@/var/run/redis.sock?db=2'
        )

    def test_url_branch_keeps_existing_userinfo(self):
        config = {'URL': 'redis://other:pw@h:6379/0', 'USERNAME': 'netbox', 'PASSWORD': 'secret'}
        self.assertEqual(settings_utils.build_rq_params(config, 300)['URL'], 'redis://other:pw@h:6379/0')

    def test_url_branch_encodes_special_characters(self):
        from redis.connection import parse_url

        config = {'URL': 'redis://h:6379/0', 'USERNAME': 'net@box', 'PASSWORD': 'p@ss:w/rd?#%'}
        url = settings_utils.build_rq_params(config, 300)['URL']
        self.assertEqual(url, 'redis://net%40box:p%40ss%3Aw%2Frd%3F%23%25@h:6379/0')
        parsed = parse_url(url)
        self.assertEqual(parsed['username'], 'net@box')
        self.assertEqual(parsed['password'], 'p@ss:w/rd?#%')
        self.assertEqual(parsed['host'], 'h')

    def test_url_branch_appends_ca_cert_once(self):
        config = {'URL': 'rediss://h:6380/0', 'SSL': True, 'CA_CERT_PATH': '/etc/ssl/ca.pem'}
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['URL'], 'rediss://h:6380/0?ssl_ca_certs=%2Fetc%2Fssl%2Fca.pem')
        # A value already present in the URL wins
        config['URL'] = 'rediss://h:6380/0?ssl_ca_certs=/other.pem'
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['URL'], 'rediss://h:6380/0?ssl_ca_certs=/other.pem')

    def test_url_branch_ca_cert_requires_tls_url(self):
        # A plain connection rejects ssl_ca_certs, so it is only added to rediss:// URLs.
        config = {'URL': 'redis://h:6379/0', 'CA_CERT_PATH': '/ca.pem'}
        self.assertEqual(settings_utils.build_rq_params(config, 300)['URL'], 'redis://h:6379/0')

    def test_url_branch_encodes_kwargs(self):
        from redis.connection import parse_url

        config = {
            'URL': 'rediss://h:6380/0',
            'KWARGS': {
                'socket_timeout': 5,
                'socket_connect_timeout': 2.5,
                'ssl_check_hostname': False,
                'health_check_interval': 30,
                'client_name': 'netbox',
            },
        }
        url = settings_utils.build_rq_params(config, 300)['URL']
        parsed = parse_url(url)
        self.assertEqual(parsed['socket_timeout'], 5.0)
        self.assertEqual(parsed['socket_connect_timeout'], 2.5)
        self.assertIs(parsed['ssl_check_hostname'], False)
        self.assertEqual(parsed['health_check_interval'], 30)
        self.assertEqual(parsed['client_name'], 'netbox')

    def test_url_branch_rejects_unencodable_kwargs(self):
        for kwargs in (
            {'socket_keepalive_options': {1: 2}},
            {'retry': object()},
            {'client_name': None},
            {'health_check_interval': 1.5},
            {'socket_keepalive': 1},
            {'max_connections': True},
            {'custom_flag': True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesMessage(ImproperlyConfigured, 'HOST and PORT'):
                    settings_utils.build_rq_params({'URL': 'redis://h/0', 'KWARGS': kwargs}, 300)

    def test_url_branch_connection_wiring(self):
        from django_rq.connection_utils import get_redis_connection

        config = {
            'URL': 'rediss://h:6380/0',
            'SSL': True,
            'USERNAME': 'netbox',
            'PASSWORD': 's3cr@t',
            'CA_CERT_PATH': '/ca.pem',
            'KWARGS': {'socket_timeout': 5},
        }
        connection = get_redis_connection(settings_utils.build_rq_params(config, 300))
        connection_kwargs = connection.connection_pool.connection_kwargs
        self.assertEqual(connection_kwargs['username'], 'netbox')
        self.assertEqual(connection_kwargs['password'], 's3cr@t')
        self.assertEqual(connection_kwargs['ssl_ca_certs'], '/ca.pem')
        self.assertEqual(connection_kwargs['socket_timeout'], 5.0)


class BuildCachesTest(SimpleTestCase):
    def test_host_branch_defaults(self):
        caches, factory = settings_utils.build_caches({})
        self.assertEqual(caches, {
            'default': {
                'BACKEND': 'django_redis.cache.RedisCache',
                'LOCATION': 'redis://localhost:6379/0',
                'OPTIONS': {
                    'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                    'USERNAME': '',
                    'PASSWORD': '',
                },
            },
        })
        self.assertEqual(factory, 'django_redis.pool.ConnectionFactory')

    def test_host_branch_with_tls_and_kwargs(self):
        config = {
            'HOST': 'redis.example.com',
            'PORT': 6380,
            'USERNAME': 'netbox',
            'PASSWORD': 'p@ss',
            'DATABASE': 3,
            'SSL': True,
            'CA_CERT_PATH': '/ca.pem',
            'KWARGS': {'socket_timeout': 5},
        }
        caches, factory = settings_utils.build_caches(config)
        self.assertEqual(caches['default']['LOCATION'], 'rediss://netbox@redis.example.com:6380/3')
        self.assertEqual(caches['default']['OPTIONS'], {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'USERNAME': 'netbox',
            'PASSWORD': 'p@ss',
            'CONNECTION_POOL_KWARGS': {'ssl_ca_certs': '/ca.pem', 'socket_timeout': 5},
        })
        self.assertEqual(factory, 'django_redis.pool.ConnectionFactory')

    def test_host_branch_insecure_skip_tls_verify(self):
        config = {'SSL': True, 'INSECURE_SKIP_TLS_VERIFY': True, 'KWARGS': {'ssl_check_hostname': False}}
        caches, _ = settings_utils.build_caches(config)
        self.assertEqual(caches['default']['LOCATION'], 'rediss://localhost:6379/0')
        self.assertEqual(caches['default']['OPTIONS']['CONNECTION_POOL_KWARGS'], {
            'ssl_cert_reqs': None,
            'ssl_check_hostname': False,
        })

    def test_url_branch(self):
        config = {'URL': 'unix:///var/run/redis.sock', 'PASSWORD': 'secret', 'HOST': 'ignored'}
        caches, factory = settings_utils.build_caches(config)
        self.assertEqual(caches['default']['LOCATION'], 'unix:///var/run/redis.sock')
        self.assertEqual(caches['default']['OPTIONS'], {
            'CLIENT_CLASS': 'django_redis.client.DefaultClient',
            'USERNAME': '',
            'PASSWORD': 'secret',
        })
        self.assertEqual(factory, 'django_redis.pool.ConnectionFactory')

    def test_sentinel_branch(self):
        config = {
            'SENTINELS': [('s1', 26379)],
            'SENTINEL_SERVICE': 'mymaster',
            'URL': 'redis://ignored/0',
            'PASSWORD': 'secret',
            'DATABASE': 2,
        }
        caches, factory = settings_utils.build_caches(config)
        self.assertEqual(factory, 'django_redis.pool.SentinelConnectionFactory')
        self.assertEqual(caches['default']['LOCATION'], 'redis://mymaster/2')
        self.assertEqual(caches['default']['OPTIONS']['CLIENT_CLASS'], 'django_redis.client.SentinelClient')
        self.assertEqual(caches['default']['OPTIONS']['SENTINELS'], [('s1', 26379)])
        self.assertEqual(caches['default']['OPTIONS']['PASSWORD'], 'secret')
        self.assertEqual(caches['default']['OPTIONS']['SOCKET_CONNECT_TIMEOUT'], 10)
        self.assertEqual(caches['default']['OPTIONS']['SENTINEL_KWARGS'], {'socket_connect_timeout': 10})

    def test_sentinel_timeout_and_credentials(self):
        config = {
            'SENTINELS': [('s1', 26379)],
            'SENTINEL_TIMEOUT': 2,
            'USERNAME': 'netbox',
            'PASSWORD': 'secret',
            'SENTINEL_AUTH': True,
        }
        caches, _ = settings_utils.build_caches(config)
        options = caches['default']['OPTIONS']
        self.assertEqual(options['SOCKET_CONNECT_TIMEOUT'], 2)
        self.assertEqual(options['SENTINEL_KWARGS'], {
            'socket_connect_timeout': 2,
            'username': 'netbox',
            'password': 'secret',
        })

    def test_empty_sentinels_is_not_sentinel_mode(self):
        caches, factory = settings_utils.build_caches({'SENTINELS': [], 'URL': 'redis://h/0'})
        self.assertEqual(factory, 'django_redis.pool.ConnectionFactory')
        self.assertEqual(caches['default']['LOCATION'], 'redis://h/0')
        self.assertNotIn('SENTINEL_KWARGS', caches['default']['OPTIONS'])

    def test_sentinel_connection_wiring(self):
        # redis-py connects lazily, so this inspects the Sentinel manager django-redis builds without network I/O.
        from django_redis.pool import SentinelConnectionFactory

        config = {
            'SENTINELS': [('s1', 26379)],
            'SENTINEL_TIMEOUT': 4,
            'PASSWORD': 'secret',
            'SENTINEL_PASSWORD': 'sentinel-secret',
        }
        caches, _ = settings_utils.build_caches(config)
        factory = SentinelConnectionFactory(dict(caches['default']['OPTIONS']))
        sentinel_node_kwargs = factory._sentinel.sentinels[0].connection_pool.connection_kwargs
        self.assertEqual(sentinel_node_kwargs['password'], 'sentinel-secret')
        self.assertEqual(sentinel_node_kwargs['socket_connect_timeout'], 4)
        self.assertEqual(factory._sentinel.connection_kwargs['password'], 'secret')
        self.assertEqual(factory._sentinel.connection_kwargs['socket_connect_timeout'], 4)

    def test_does_not_mutate_config(self):
        kwargs = {'socket_timeout': 5}
        config = {'KWARGS': kwargs, 'CA_CERT_PATH': '/ca.pem'}
        caches, _ = settings_utils.build_caches(config)
        caches['default']['OPTIONS']['CONNECTION_POOL_KWARGS']['extra'] = True
        self.assertEqual(kwargs, {'socket_timeout': 5})


class SentinelKwargsTest(SimpleTestCase):
    def test_defaults(self):
        self.assertEqual(settings_utils.build_sentinel_kwargs({}), {'socket_connect_timeout': 10})

    def test_sentinel_timeout(self):
        self.assertEqual(
            settings_utils.build_sentinel_kwargs({'SENTINEL_TIMEOUT': 3}),
            {'socket_connect_timeout': 3},
        )

    def test_explicit_credentials(self):
        config = {'SENTINEL_USERNAME': 'sentinel-user', 'SENTINEL_PASSWORD': 'sentinel-secret'}
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {
            'socket_connect_timeout': 10,
            'username': 'sentinel-user',
            'password': 'sentinel-secret',
        })

    def test_explicit_password_only(self):
        config = {'SENTINEL_PASSWORD': 'sentinel-secret'}
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {
            'socket_connect_timeout': 10,
            'password': 'sentinel-secret',
        })

    def test_empty_values_are_omitted(self):
        config = {
            'SENTINEL_USERNAME': '',
            'SENTINEL_PASSWORD': '',
            'SENTINEL_AUTH': True,
            'USERNAME': '',
            'PASSWORD': None,
        }
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {'socket_connect_timeout': 10})

    def test_sentinel_auth_reuses_data_node_credentials(self):
        config = {'SENTINEL_AUTH': True, 'USERNAME': 'netbox', 'PASSWORD': 'secret'}
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {
            'socket_connect_timeout': 10,
            'username': 'netbox',
            'password': 'secret',
        })

    def test_sentinel_auth_explicit_keys_override(self):
        config = {
            'SENTINEL_AUTH': True,
            'USERNAME': 'netbox',
            'PASSWORD': 'secret',
            'SENTINEL_PASSWORD': 'sentinel-secret',
        }
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {
            'socket_connect_timeout': 10,
            'username': 'netbox',
            'password': 'sentinel-secret',
        })

    def test_data_node_credentials_not_sent_by_default(self):
        config = {'USERNAME': 'netbox', 'PASSWORD': 'secret'}
        kwargs = settings_utils.build_sentinel_kwargs(config)
        self.assertNotIn('username', kwargs)
        self.assertNotIn('password', kwargs)
        config['SENTINEL_AUTH'] = False
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {'socket_connect_timeout': 10})

    def test_sentinel_kwargs_merged_with_dedicated_keys_winning(self):
        config = {
            'SENTINEL_KWARGS': {
                'ssl': True,
                'ssl_ca_certs': '/ca.pem',
                'socket_connect_timeout': 99,
                'username': 'kwargs-user',
                'password': 'kwargs-secret',
            },
            'SENTINEL_TIMEOUT': 5,
            'SENTINEL_AUTH': True,
            'USERNAME': 'netbox',
            'SENTINEL_PASSWORD': 'sentinel-secret',
        }
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {
            'ssl': True,
            'ssl_ca_certs': '/ca.pem',
            'socket_connect_timeout': 5,
            'username': 'netbox',
            'password': 'sentinel-secret',
        })

    def test_sentinel_kwargs_credentials_kept_without_overrides(self):
        config = {'SENTINEL_KWARGS': {'password': 'kwargs-secret'}, 'PASSWORD': 'secret'}
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {
            'socket_connect_timeout': 10,
            'password': 'kwargs-secret',
        })

    def test_kwargs_socket_options_seed_sentinel_kwargs(self):
        config = {'KWARGS': {'socket_timeout': 5, 'socket_keepalive': True, 'ssl_check_hostname': False}}
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {
            'socket_timeout': 5,
            'socket_keepalive': True,
            'socket_connect_timeout': 10,
        })

    def test_sentinel_kwargs_override_kwargs_socket_options(self):
        config = {
            'KWARGS': {'socket_timeout': 5, 'socket_connect_timeout': 1},
            'SENTINEL_KWARGS': {'socket_timeout': 2, 'socket_connect_timeout': 7},
            'SENTINEL_TIMEOUT': 3,
        }
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {
            'socket_timeout': 2,
            'socket_connect_timeout': 3,
        })

    def test_sentinel_timeout_wins_over_kwargs_socket_connect_timeout(self):
        config = {'KWARGS': {'socket_connect_timeout': 1}, 'SENTINEL_TIMEOUT': 4}
        self.assertEqual(settings_utils.build_sentinel_kwargs(config), {'socket_connect_timeout': 4})

    def test_tasks_sentinel_kwargs_include_kwargs_socket_options(self):
        config = {'SENTINELS': [('s1', 26379)], 'KWARGS': {'socket_timeout': 5, 'health_check_interval': 30}}
        params = settings_utils.build_rq_params(config, 300)
        self.assertEqual(params['SENTINEL_KWARGS'], {'socket_timeout': 5, 'socket_connect_timeout': 10})

    def test_caching_sentinel_kwargs_include_kwargs_socket_options(self):
        from django_redis.pool import SentinelConnectionFactory

        config = {
            'SENTINELS': [('s1', 26379)],
            'KWARGS': {'socket_timeout': 5},
            'SENTINEL_KWARGS': {'socket_keepalive': True},
        }
        caches, _ = settings_utils.build_caches(config)
        self.assertEqual(caches['default']['OPTIONS']['SENTINEL_KWARGS'], {
            'socket_timeout': 5,
            'socket_keepalive': True,
            'socket_connect_timeout': 10,
        })
        # Matches what redis-py would have copied to the Sentinel nodes without explicit sentinel_kwargs
        factory = SentinelConnectionFactory(dict(caches['default']['OPTIONS']))
        sentinel_node_kwargs = factory._sentinel.sentinels[0].connection_pool.connection_kwargs
        self.assertEqual(sentinel_node_kwargs['socket_timeout'], 5)
        self.assertIs(sentinel_node_kwargs['socket_keepalive'], True)

    def test_input_not_mutated(self):
        sentinel_kwargs = {'ssl': True}
        config = {'SENTINEL_KWARGS': sentinel_kwargs, 'SENTINEL_PASSWORD': 'sentinel-secret'}
        settings_utils.build_sentinel_kwargs(config)
        self.assertEqual(sentinel_kwargs, {'ssl': True})
        self.assertEqual(config, {'SENTINEL_KWARGS': {'ssl': True}, 'SENTINEL_PASSWORD': 'sentinel-secret'})


class EmbedRedisUrlCredentialsTest(SimpleTestCase):
    def test_no_changes_returns_url_unchanged(self):
        for url in ('redis://h:6379/0', 'unix:///var/run/redis.sock', 'rediss://[::1]:6380/0?db=1'):
            with self.subTest(url=url):
                self.assertEqual(settings_utils.embed_redis_url_credentials(url, '', '', {}), url)

    def test_ipv6_host(self):
        url = settings_utils.embed_redis_url_credentials('redis://[::1]:6379/0', 'netbox', 'secret', {})
        self.assertEqual(url, 'redis://netbox:secret@[::1]:6379/0')

    def test_password_only(self):
        url = settings_utils.embed_redis_url_credentials('redis://h:6379/0', '', 'secret', {})
        self.assertEqual(url, 'redis://:secret@h:6379/0')

    def test_username_only(self):
        url = settings_utils.embed_redis_url_credentials('redis://h:6379/0', 'netbox', '', {})
        self.assertEqual(url, 'redis://netbox@h:6379/0')

    def test_existing_query_preserved(self):
        url = settings_utils.embed_redis_url_credentials(
            'rediss://h:6380/0?ssl_cert_reqs=none&socket_timeout=1',
            '',
            '',
            {'socket_timeout': '5', 'ssl_ca_certs': '/ca.pem'},
        )
        self.assertEqual(url, 'rediss://h:6380/0?ssl_cert_reqs=none&socket_timeout=1&ssl_ca_certs=%2Fca.pem')

    def test_unix_socket(self):
        url = settings_utils.embed_redis_url_credentials(
            'unix:///var/run/redis.sock', 'netbox', 'secret', {'db': '3'}
        )
        self.assertEqual(url, 'unix://netbox:secret@/var/run/redis.sock?db=3')
