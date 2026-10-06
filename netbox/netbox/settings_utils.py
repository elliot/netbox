"""Startup helpers for settings.py. Import-safe: no Django settings access at import time."""

import importlib
import importlib.util
import os
import sys
import threading
import warnings
from typing import NamedTuple
from urllib.parse import parse_qs, quote, urlencode, urlsplit

from django.core.exceptions import ImproperlyConfigured
from redis.connection import URL_QUERY_ARGUMENT_PARSERS, to_bool
from rq.exceptions import TimeoutFormatError
from rq.queue import Queue
from rq.utils import parse_timeout

__all__ = (
    'InstallPaths',
    'build_caches',
    'build_rq_params',
    'build_sentinel_kwargs',
    'embed_redis_url_credentials',
    'get_configuration_dir',
    'load_configuration',
    'load_ldap_config',
    'parse_job_timeout',
    'resolve_install_paths',
    'secret_key_hint',
    'uses_sentinel',
    'validate_webhook_default_timeout',
)


class InstallPaths(NamedTuple):
    """Filesystem layout resolved from the install mode (wheel vs. source checkout)."""
    install_mode: str      # 'wheel' or 'checkout'
    base_dir: str          # package data root (BASE_DIR)
    netbox_root: str       # instance root for mutable files (NETBOX_ROOT)
    docs_root: str         # documentation sources on a checkout, the pre-rendered site in a wheel (DOCS_ROOT default)
    static_docs_root: str  # built documentation, source of the STATICFILES 'docs' prefix


def resolve_install_paths(settings_dir, environ):
    """Resolve the install mode and filesystem roots for this NetBox installation.

    A wheel bundles package data (including the pre-rendered documentation site)
    under netbox/_data and keeps mutable instance files under an external instance root
    (NETBOX_ROOT, default /opt/netbox); a source checkout keeps the historical layout,
    where both roots are the project directory. All wheel-vs-checkout branching lives
    here so settings.py stays declarative.
    """
    bundled_data = os.path.join(settings_dir, '_data')
    if os.path.isdir(bundled_data):
        install_mode = 'wheel'
        base_dir = bundled_data
        netbox_root = os.path.abspath(environ.get('NETBOX_ROOT', '/opt/netbox'))
        docs_root = os.path.join(base_dir, 'docs')
        # The wheel bundles the pre-rendered documentation site at _data/docs; it serves as
        # both the DOCS_ROOT default and the STATICFILES 'docs' prefix source.
        static_docs_root = docs_root
    else:
        install_mode = 'checkout'
        base_dir = os.path.dirname(settings_dir)
        netbox_root = base_dir
        docs_root = os.path.join(os.path.dirname(base_dir), 'docs')
        static_docs_root = os.path.join(base_dir, 'project-static', 'docs')
    return InstallPaths(
        install_mode=install_mode,
        base_dir=base_dir,
        netbox_root=netbox_root,
        docs_root=docs_root,
        static_docs_root=static_docs_root,
    )


def secret_key_hint(install_mode, base_dir):
    """Return the command to suggest in the SECRET_KEY-too-short error, based on install mode.

    generate_secret_key.py is not packaged in a wheel, so a wheel install points at the
    `netbox secret-key` console command instead of the (nonexistent) script path.
    """
    if install_mode == 'wheel':
        return 'netbox secret-key'
    return f'python {base_dir}/generate_secret_key.py'


def parse_job_timeout(value):
    """Normalize an RQ job timeout (i.e. RQ_DEFAULT_TIMEOUT) to a number of seconds.

    RQ accepts a timeout as an integer, as a numeric string, or as a duration string such as
    "1h", so its own parser is used to arrive at a value which can be compared against webhook
    timeouts. A negative timeout (-1 by convention) disables RQ's death penalty; that is reported
    as None, meaning that job execution is unbounded. An absent or zero timeout is *not* unbounded:
    RQ falls back to the queue's own default, which is reported in its place.
    """
    try:
        timeout = parse_timeout(value)
    except (TimeoutFormatError, TypeError):
        raise ImproperlyConfigured(
            f"RQ_DEFAULT_TIMEOUT must be a number of seconds or a duration string such as '1h' "
            f"(found {value!r})"
        )
    if timeout is None or timeout == 0:
        # Queue treats a null or zero default timeout as unset and substitutes its class default.
        return Queue.DEFAULT_TIMEOUT
    if timeout < 0:
        return None
    return timeout


def validate_webhook_default_timeout(timeout, job_timeout):
    """Validate WEBHOOK_DEFAULT_TIMEOUT, including against the background job timeout.

    job_timeout is the normalized RQ_DEFAULT_TIMEOUT (see parse_job_timeout()), or None if job
    execution is unbounded. A webhook timeout which meets or exceeds the job timeout leaves no
    room for the request's own timeout to apply, as the worker will terminate the job first.
    """
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        raise ImproperlyConfigured(
            f"WEBHOOK_DEFAULT_TIMEOUT must be an integer between 1 and 3600 (found {timeout!r})"
        )
    if job_timeout is not None and timeout >= job_timeout:
        raise ImproperlyConfigured(
            f"WEBHOOK_DEFAULT_TIMEOUT ({timeout}) must be less than RQ_DEFAULT_TIMEOUT ({job_timeout} seconds), "
            f"which caps the total runtime of the background job."
        )


def _import_module(name):
    """Import a configuration module by dotted path.

    Preserve NetBox's historical behavior: a friendly ImproperlyConfigured when the module
    itself is absent, but re-raise the original error when the module exists yet imports
    something else that is missing.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as e:
        if e.name == name:
            raise ImproperlyConfigured(
                f"Specified configuration module ({name}) not found. Please define "
                f"netbox/netbox/configuration.py per the documentation, or specify an alternate "
                f"module in the NETBOX_CONFIGURATION environment variable."
            )
        raise


# Serializes cache checks, module execution, and the temporary sys.path change.
# Reentrant because configuration code may load another path-based module.
_import_lock = threading.RLock()


def _import_from_path(module_name, path):
    """Load a configuration module from an explicit file path.

    The module is registered in sys.modules while it executes, and the file's directory is
    placed on sys.path for that duration so the module can import siblings, matching normal
    import semantics closely enough for configuration files. A module already loaded under the
    same name from the same path is reused, while the same name from a different path replaces
    it. A failed load leaves the previous entry in place. Loading is serialized so that a
    concurrent caller cannot observe a module mid-execution.
    """
    path = os.path.abspath(path)
    with _import_lock:
        existing = sys.modules.get(module_name)
        existing_path = getattr(existing, '__file__', None)
        if existing_path and os.path.abspath(existing_path) == path:
            return existing
        module_dir = os.path.dirname(path)
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImproperlyConfigured(f"Unable to load configuration file {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        sys.path.insert(0, module_dir)
        try:
            spec.loader.exec_module(module)
        except Exception:
            if sys.modules.get(module_name) is module:
                if existing is None:
                    del sys.modules[module_name]
                else:
                    sys.modules[module_name] = existing
            raise
        finally:
            # Remove only the entry this helper inserted at index 0.
            if sys.path and sys.path[0] == module_dir:
                sys.path.pop(0)
        return module


def get_configuration_dir(module):
    """Return the directory containing a loaded configuration module (None if unknown)."""
    source = getattr(module, '__file__', None)
    return os.path.dirname(os.path.abspath(source)) if source else None


def load_configuration(*, install_mode, install_root, environ):
    """Import and return NetBox's configuration module.

    An explicit NETBOX_CONFIGURATION module always wins. In wheel mode, prefer
    <install_root>/conf/configuration.py, loaded by file path (so a stale source tree at
    <install_root>/netbox cannot shadow it and no generic 'configuration' module is left in
    sys.modules), then
    fall back to the legacy <install_root>/netbox/netbox/configuration.py with a migration
    warning. In checkout mode, keep the historical default module.
    """
    explicit = environ.get('NETBOX_CONFIGURATION')
    if explicit:
        return _import_module(explicit)

    if install_mode == 'wheel':
        conf_dir = os.path.join(install_root, 'conf')
        preferred = os.path.join(conf_dir, 'configuration.py')
        legacy = os.path.join(install_root, 'netbox', 'netbox', 'configuration.py')
        if os.path.isfile(preferred):
            if os.path.isfile(legacy):
                warnings.warn(
                    f"Both {preferred} and the legacy {legacy} exist; using {preferred} and "
                    f"ignoring the legacy file.",
                    RuntimeWarning,
                )
            return _import_from_path('netbox_local_configuration', preferred)
        if os.path.isfile(legacy):
            warnings.warn(
                f"Loaded NetBox configuration from the legacy source-tree path {legacy}. For a "
                f"pip-installed NetBox, move it to {preferred}.",
                RuntimeWarning,
            )
            return _import_from_path('netbox_legacy_configuration', legacy)
        raise ImproperlyConfigured(
            f"No NetBox configuration found. For a pip-installed NetBox, create {preferred}, "
            f"or set NETBOX_CONFIGURATION to an importable module."
        )

    return _import_module('netbox.configuration')


def load_ldap_config(config_dir, *, allow_legacy_fallback=False):
    """Load ldap_config.py from the active configuration directory (settings.CONFIGURATION_DIR).

    One rule for every install method: the active ldap_config.py is the one next to the
    active configuration.py. Checkout installs may additionally allow a legacy fallback to
    the historical netbox/netbox/ldap_config.py module, because a custom NETBOX_CONFIGURATION
    can live outside the source tree while LDAP config stayed inside it; the fallback warns
    so those installs can migrate to the sibling rule.
    """
    path = os.path.join(config_dir, 'ldap_config.py') if config_dir else None
    if path and os.path.isfile(path):
        return _import_from_path('netbox.ldap_config', path)
    if allow_legacy_fallback:
        try:
            module = importlib.import_module('netbox.ldap_config')
        except ModuleNotFoundError as e:
            if e.name != 'netbox.ldap_config':
                raise
        else:
            warnings.warn(
                "Loaded LDAP configuration from the legacy netbox/netbox/ldap_config.py module. "
                "Move ldap_config.py into the directory containing the active configuration.py; "
                "this fallback may be removed in a future release.",
                RuntimeWarning,
            )
            return module
    if not config_dir:
        raise ImproperlyConfigured(
            "LDAP configuration file not found: unable to determine the directory containing "
            "configuration.py."
        )
    raise ImproperlyConfigured(
        "LDAP configuration file not found: Check that ldap_config.py has been created "
        "alongside configuration.py. For a pip-installed NetBox, this is "
        "NETBOX_ROOT/conf/ldap_config.py."
    )


#
# Redis
#

def uses_sentinel(config):
    """Return True if a REDIS subsection (tasks or caching) is configured to use Redis Sentinel."""
    sentinels = config.get('SENTINELS', [])
    return isinstance(sentinels, (list, tuple)) and len(sentinels) > 0


def _credentials(config, user_key, pass_key):
    """Return the username/password connection kwargs held under the given keys, omitting empty values."""
    credentials = {}
    if username := config.get(user_key):
        credentials['username'] = username
    if password := config.get(pass_key):
        credentials['password'] = password
    return credentials


def build_sentinel_kwargs(config):
    """Build the connection kwargs for the Sentinel nodes themselves (redis-py's sentinel_kwargs).

    Lowest to highest precedence: any socket_* options in KWARGS (which redis-py itself copies to the
    Sentinel nodes when no sentinel_kwargs are given), SENTINEL_KWARGS, SENTINEL_TIMEOUT (always applied
    as the connect timeout), the data-node USERNAME/PASSWORD (only if SENTINEL_AUTH is enabled), then
    SENTINEL_USERNAME/SENTINEL_PASSWORD. Data-node credentials are never sent to Sentinel unless
    SENTINEL_AUTH is enabled.
    """
    kwargs = {key: value for key, value in (config.get('KWARGS') or {}).items() if key.startswith('socket_')}
    kwargs.update(config.get('SENTINEL_KWARGS') or {})
    kwargs['socket_connect_timeout'] = config.get('SENTINEL_TIMEOUT', 10)
    if config.get('SENTINEL_AUTH', False):
        kwargs.update(_credentials(config, 'USERNAME', 'PASSWORD'))
    kwargs.update(_credentials(config, 'SENTINEL_USERNAME', 'SENTINEL_PASSWORD'))
    return kwargs


def _ssl_kwargs(config):
    """Return the TLS connection kwargs for the Redis data node (empty unless SSL is enabled)."""
    if not config.get('SSL', False):
        return {}
    kwargs = {
        'ssl': True,
        'ssl_cert_reqs': None if config.get('INSECURE_SKIP_TLS_VERIFY', False) else 'required',
    }
    if ca_cert_path := config.get('CA_CERT_PATH'):
        kwargs['ssl_ca_certs'] = ca_cert_path
    return kwargs


def embed_redis_url_credentials(url, username, password, query):
    """Return a Redis URL augmented with credentials and query parameters.

    Anything already in the URL wins. The username and password are each percent-encoded and added
    only if the URL does not already provide that field, either in its userinfo or as a query
    parameter (redis-py lets userinfo override the query, so adding it there would invert this).
    Query parameters are added only where the URL does not already set them.
    Handles redis://, rediss:// and unix:// URLs, including IPv6 hosts.
    """
    parts = urlsplit(url)
    existing = parse_qs(parts.query, keep_blank_values=True)

    # Work on the raw (still percent-encoded) userinfo so that existing values are kept verbatim.
    raw_userinfo, has_userinfo, hostport = parts.netloc.rpartition('@')
    if not has_userinfo:
        hostport = parts.netloc
    raw_username, has_password, raw_password = raw_userinfo.partition(':')
    if username and not raw_username and 'username' not in existing:
        raw_username = quote(str(username), safe='')
    if password and not raw_password and 'password' not in existing:
        raw_password = quote(str(password), safe='')
        has_password = ':'
    netloc = hostport
    if raw_username or has_password or has_userinfo:
        netloc = f"{raw_username}{has_password}{raw_password}@{hostport}"

    extra = urlencode([(key, value) for key, value in query.items() if key not in existing])
    if netloc == parts.netloc and not extra:
        return url
    # Assembled by hand: urlunsplit() drops the empty authority of unix:///path URLs.
    new_url = f'{parts.scheme}://{netloc}{parts.path}'
    if new_query := '&'.join(filter(None, [parts.query, extra])):
        new_url += f'?{new_query}'
    if parts.fragment:
        new_url += f'#{parts.fragment}'
    return new_url


def _url_safe_kwargs(kwargs):
    """Convert KWARGS to Redis URL query parameters, or raise ImproperlyConfigured if that is not possible.

    redis-py parses query parameters back into Python values for the keys listed in
    URL_QUERY_ARGUMENT_PARSERS; any other key is passed through as a string.
    """
    query = {}
    for key, value in kwargs.items():
        parser = URL_QUERY_ARGUMENT_PARSERS.get(key)
        if isinstance(value, str):
            encodable = True
        elif isinstance(value, bool):
            encodable = parser is to_bool
        elif isinstance(value, int):
            encodable = parser in (int, float)
        elif isinstance(value, float):
            encodable = parser is float
        else:
            encodable = False
        if not encodable:
            raise ImproperlyConfigured(
                f"REDIS['tasks']['KWARGS'][{key!r}] (of type {type(value).__name__}) cannot be encoded in "
                f"the Redis URL. Configure the connection using HOST and PORT instead of URL to pass this option."
            )
        query[key] = str(value)
    return query


def build_rq_params(config, default_timeout):
    """Build the django-rq connection parameters (RQ_PARAMS) from REDIS['tasks'].

    Sentinel takes precedence over URL, which takes precedence over HOST/PORT. django-rq honours
    only CONNECTION_KWARGS and SENTINEL_KWARGS in Sentinel mode, and only the URL itself in URL
    mode, so the TLS options, credentials and KWARGS are carried there for those modes.
    """
    ssl_cert_reqs = None if config.get('INSECURE_SKIP_TLS_VERIFY', False) else 'required'
    if uses_sentinel(config):
        params = {
            'SENTINELS': config['SENTINELS'],
            'MASTER_NAME': config.get('SENTINEL_SERVICE', 'default'),
            'SOCKET_TIMEOUT': None,
            'CONNECTION_KWARGS': {
                'socket_connect_timeout': config.get('SENTINEL_TIMEOUT', 10),
                **_ssl_kwargs(config),
                **(config.get('KWARGS') or {}),
            },
            'SENTINEL_KWARGS': build_sentinel_kwargs(config),
        }
    elif url := config.get('URL'):
        query = {}
        kwargs = config.get('KWARGS') or {}
        # TLS options are only accepted by TLS connections, i.e. a rediss:// URL
        is_tls_url = urlsplit(url).scheme.lower() == 'rediss'
        if (ca_cert_path := config.get('CA_CERT_PATH')) and is_tls_url:
            query['ssl_ca_certs'] = ca_cert_path
        if not is_tls_url and (ssl_keys := sorted(key for key in kwargs if key.startswith('ssl_'))):
            raise ImproperlyConfigured(
                f"REDIS['tasks']['KWARGS'] sets TLS options ({', '.join(ssl_keys)}) but URL does not use the "
                f"rediss:// scheme. Use a rediss:// URL, or remove these options."
            )
        query.update(_url_safe_kwargs(kwargs))
        params = {
            'URL': embed_redis_url_credentials(
                url, config.get('USERNAME', ''), config.get('PASSWORD', ''), query
            ),
            'SSL': config.get('SSL', False),
            'SSL_CERT_REQS': ssl_cert_reqs,
        }
    else:
        params = {
            'HOST': config.get('HOST', 'localhost'),
            'PORT': config.get('PORT', 6379),
            'SSL': config.get('SSL', False),
            'SSL_CERT_REQS': ssl_cert_reqs,
        }
    params.update({
        'DB': config.get('DATABASE', 0),
        'USERNAME': config.get('USERNAME', ''),
        'PASSWORD': config.get('PASSWORD', ''),
        'DEFAULT_TIMEOUT': default_timeout,
    })
    if ca_cert_path := config.get('CA_CERT_PATH', False):
        params.setdefault('REDIS_CLIENT_KWARGS', {})
        params['REDIS_CLIENT_KWARGS']['ssl_ca_certs'] = ca_cert_path
    # Merge in KWARGS for additional parameters
    if kwargs := config.get('KWARGS'):
        params.setdefault('REDIS_CLIENT_KWARGS', {})
        params['REDIS_CLIENT_KWARGS'].update(kwargs)
    return params


def build_caches(config):
    """Build Django's CACHES setting and the django-redis connection factory from REDIS['caching'].

    Returns a (CACHES, DJANGO_REDIS_CONNECTION_FACTORY) tuple. Sentinel takes precedence over URL,
    which takes precedence over HOST/PORT.
    """
    username = config.get('USERNAME', '')
    database = config.get('DATABASE', 0)
    proto = 'rediss' if config.get('SSL', False) else 'redis'
    username_host = '@'.join(filter(None, [username, config.get('HOST', 'localhost')]))
    location = config.get('URL', f"{proto}://{username_host}:{config.get('PORT', 6379)}/{database}")
    factory = 'django_redis.pool.ConnectionFactory'
    options = {
        'CLIENT_CLASS': 'django_redis.client.DefaultClient',
        'USERNAME': username,
        'PASSWORD': config.get('PASSWORD', ''),
    }

    if uses_sentinel(config):
        factory = 'django_redis.pool.SentinelConnectionFactory'
        location = f"{proto}://{config.get('SENTINEL_SERVICE', 'default')}/{database}"
        options['CLIENT_CLASS'] = 'django_redis.client.SentinelClient'
        options['SENTINELS'] = config['SENTINELS']
        options['SOCKET_CONNECT_TIMEOUT'] = config.get('SENTINEL_TIMEOUT', 10)
        options['SENTINEL_KWARGS'] = build_sentinel_kwargs(config)
    if config.get('INSECURE_SKIP_TLS_VERIFY', False):
        options.setdefault('CONNECTION_POOL_KWARGS', {})
        options['CONNECTION_POOL_KWARGS']['ssl_cert_reqs'] = None
    if ca_cert_path := config.get('CA_CERT_PATH', False):
        options.setdefault('CONNECTION_POOL_KWARGS', {})
        options['CONNECTION_POOL_KWARGS']['ssl_ca_certs'] = ca_cert_path
    # Merge in KWARGS for additional parameters
    if kwargs := config.get('KWARGS'):
        options.setdefault('CONNECTION_POOL_KWARGS', {})
        options['CONNECTION_POOL_KWARGS'].update(kwargs)

    caches = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': location,
            'OPTIONS': options,
        }
    }
    return caches, factory
