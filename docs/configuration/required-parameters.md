# Required Configuration Settings

## ALLOWED_HOSTS

This is a list of valid fully-qualified domain names (FQDNs) and/or IP addresses that can be used to reach the NetBox service. Usually this is the same as the hostname for the NetBox server, but can also be different; for example, when using a reverse proxy serving the NetBox website under a different FQDN than the hostname of the NetBox server. To help guard against [HTTP Host header attacks](https://docs.djangoproject.com/en/stable/topics/security/#host-headers-virtual-hosting), NetBox will not permit access to the server via any other hostnames (or IPs).

!!! note
    This parameter must always be defined as a list or tuple, even if only a single value is provided.

The value of this option is also used to set `CSRF_TRUSTED_ORIGINS`, which restricts POST requests to the same set of hosts (more about this [here](https://docs.djangoproject.com/en/stable/ref/settings/#std:setting-CSRF_TRUSTED_ORIGINS)). Keep in mind that NetBox, by default, sets `USE_X_FORWARDED_HOST` to `True`, which means that if you're using a reverse proxy, it's the FQDN used to reach that reverse proxy which needs to be in this list (more about this [here](https://docs.djangoproject.com/en/stable/ref/settings/#allowed-hosts)).

Example:

```
ALLOWED_HOSTS = ['netbox.example.com', '192.0.2.123']
```

If you are not yet sure what the domain name and/or IP address of the NetBox installation will be, and are comfortable accepting the risks in doing so, you can set this to a wildcard (asterisk) to allow all host values:

```
ALLOWED_HOSTS = ['*']
```

---

## API_TOKEN_PEPPERS

[Cryptographic peppers](https://en.wikipedia.org/wiki/Pepper_(cryptography)) are employed to generate hashes of sensitive values on the server. This parameter defines the peppers used to hash v2 API tokens in NetBox. You must define at least one pepper before creating a v2 API token. See the [API documentation](../integrations/rest-api.md#authentication) for further information about how peppers are used.

```python
API_TOKEN_PEPPERS = {
    # DO NOT USE THIS EXAMPLE PEPPER IN PRODUCTION
    1: 'kp7ht*76fiQAhUi5dHfASLlYUE_S^gI^(7J^K5M!LfoH@vl&b_',
}
```

!!! warning "Peppers are sensitive"
    Treat pepper values as extremely sensitive. Consider populating peppers from environment variables at initialization time rather than defining them in the configuration file, if feasible. 

Peppers must be at least 50 characters in length and should comprise a random string with a diverse character set. Consider using the Python script at `$INSTALL_ROOT/netbox/generate_secret_key.py` to generate a pepper value. For a Python package installation, run the virtual environment's `netbox secret-key` command instead.

It is recommended to start with a pepper ID of `1`. Additional peppers can be introduced later as needed to begin rotating token hashes.

!!! tip
    Although NetBox will run without `API_TOKEN_PEPPERS` defined, the use of v2 API tokens will be unavailable.

---

## DATABASE

!!! warning "Legacy Configuration Parameter"
    The `DATABASE` configuration parameter is deprecated and will be removed in a future release. Users are advised to adopt the new `DATABASES` (plural) parameter, which allows for the configuration of multiple databases.

See the [`DATABASES`](#databases) configuration below for usage.

---

## DATABASES

NetBox requires access to a PostgreSQL 15 or later database service to store data. This service can run locally on the NetBox server or on a remote system. Databases are defined as named dictionaries:

```python
DATABASES = {
    'default': {...},
    'external1': {...},
    'external2': {...},
}
```

NetBox itself requires only that a `default` database is defined. However, certain plugins may require the configuration of additional databases. (Consider also configuring the [`DATABASE_ROUTERS`](./system.md#database_routers) parameter when multiple databases are in use.)

The following parameters must be defined for each database:

* `NAME` - Database name
* `USER` - PostgreSQL username
* `PASSWORD` - PostgreSQL password
* `HOST` - Name or IP address of the database server (use `localhost` if running locally)
* `PORT` - TCP port of the PostgreSQL service; leave blank for default port (TCP/5432)
* `CONN_MAX_AGE` - Lifetime of a [persistent database connection](https://docs.djangoproject.com/en/stable/ref/databases/#persistent-connections), in seconds (300 is the default)
* `ENGINE` - The database backend to use; must be a PostgreSQL-compatible backend (e.g. `django.db.backends.postgresql`)

Example:

```python
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': 'netbox',               # Database name
        'USER': 'netbox',               # PostgreSQL username
        'PASSWORD': 'J5brHrAXFLQSif0K', # PostgreSQL password
        'HOST': 'localhost',            # Database server
        'PORT': '',                     # Database port (leave blank for default)
        'CONN_MAX_AGE': 300,            # Max database connection age
    }
}
```

!!! note
    NetBox supports all PostgreSQL database options supported by the underlying Django framework. For a complete list of available parameters, please see [the Django documentation](https://docs.djangoproject.com/en/stable/ref/settings/#databases).

!!! warning
    The `ENGINE` parameter must specify a PostgreSQL-compatible database backend. If not defined, the default engine `django.db.backends.postgresql` will be used.

---

## REDIS

[Redis](https://redis.io/) is a lightweight in-memory data store similar to memcached. NetBox employs Redis for background task queuing and other features.

Redis is configured using a configuration setting similar to `DATABASE` and these settings are the same for both of the `tasks` and `caching` subsections:

* `HOST` - Name or IP address of the Redis server (use `localhost` if running locally)
* `PORT` - TCP port of the Redis service; leave blank for default port (6379)
* `USERNAME` - Redis username (if set)
* `PASSWORD` - Redis password (if set)
* `DATABASE` - Numeric database ID
* `SSL` - Use SSL connection to Redis
* `INSECURE_SKIP_TLS_VERIFY` - Set to `True` to **disable** TLS certificate verification (not recommended)

An example configuration is provided below:

```python
REDIS = {
    'tasks': {
        'HOST': 'redis.example.com',
        'PORT': 1234,
        'USERNAME': 'netbox',
        'PASSWORD': 'foobar',
        'DATABASE': 0,
        'SSL': False,
    },
    'caching': {
        'HOST': 'localhost',
        'PORT': 6379,
        'USERNAME': '',
        'PASSWORD': '',
        'DATABASE': 1,
        'SSL': False,
    }
}
```

!!! warning
    It is highly recommended to keep the task and cache databases separate. Using the same database number on the
    same Redis instance for both may result in queued background tasks being lost during cache flushing events.

!!! danger "Redis is a trusted component"
    NetBox's background workers deserialize and execute jobs read from the `tasks` Redis database, so any party with write access to it can run arbitrary code on a worker. Redis must be treated as trusted infrastructure, on par with the PostgreSQL database: keep it bound to a private network and require authentication.

### UNIX Socket Support

Redis may alternatively be configured by specifying a complete URL instead of individual components. This approach supports the use of a UNIX socket connection. For example:

```python
REDIS = {
    'tasks': {
        'URL': 'unix:///run/redis-netbox/redis.sock?db=0'
    },
    'caching': {
        'URL': 'unix:///run/redis-netbox/redis.sock?db=1'
    },
}
```

`URL` takes precedence over `HOST` and `PORT`, while `SENTINELS` (if set) takes precedence over `URL`. The URL may use the `redis://`, `rediss://` (TLS), or `unix://` scheme.

When a URL is used for `tasks`, the other connection options are merged into it:

* `USERNAME` and `PASSWORD` are percent-encoded and added to the URL, unless the URL already includes credentials of its own.
* `CA_CERT_PATH` is added as the `ssl_ca_certs` query parameter for `rediss://` URLs.
* Each `KWARGS` item is added as a query parameter. Strings are always accepted. Numbers and booleans are accepted only for the parameters which redis-py converts back from a URL (for example `socket_timeout`, `socket_connect_timeout`, `health_check_interval` or `ssl_check_hostname`). Any other value (for example a dictionary or `None`) raises a configuration error at startup; use `HOST` and `PORT` instead of `URL` to pass such options.

Anything already present in the URL, whether credentials or a query parameter, wins over the corresponding option. For `caching`, `USERNAME`, `PASSWORD`, `CA_CERT_PATH` and `KWARGS` are passed to django-redis alongside the URL.

### Using Redis Sentinel

If you are using [Redis Sentinel](https://redis.io/topics/sentinel) for high-availability purposes, there is minimal 
configuration necessary to convert NetBox to recognize it. It requires the removal of the `HOST` and `PORT` keys from 
above and the addition of three new keys.

* `SENTINELS`: List of tuples or tuple of tuples with each inner tuple containing the name or IP address 
of the Redis server and port for each sentinel instance to connect to
* `SENTINEL_SERVICE`: Name of the master / service to connect to
* `SENTINEL_TIMEOUT`: Connection timeout, in seconds (default: `10`). This applies both to the Sentinel nodes and to the Redis server they point to.

The `USERNAME`, `PASSWORD`, `SSL`, `CA_CERT_PATH`, `INSECURE_SKIP_TLS_VERIFY` and `KWARGS` keys apply to the Redis server (the master) which Sentinel points to, not to the Sentinel nodes themselves. The exception is any `socket_*` options in `KWARGS`, which apply to both (see below).

Example:

```python
REDIS = {
    'tasks': {
        'SENTINELS': [('mysentinel.redis.example.com', 6379)],
        'SENTINEL_SERVICE': 'netbox',
        'SENTINEL_TIMEOUT': 10,
        'PASSWORD': '',
        'DATABASE': 0,
        'SSL': False,
    },
    'caching': {
        'SENTINELS': [
            ('mysentinel.redis.example.com', 6379),
            ('othersentinel.redis.example.com', 6379)
        ],
        'SENTINEL_SERVICE': 'netbox',
        'PASSWORD': '',
        'DATABASE': 1,
        'SSL': False,
    }
}
```

!!! note
    It is permissible to use Sentinel for only one database and not the other.

#### Sentinel Authentication

Sentinel nodes are configured independently of the Redis servers they monitor (`sentinel.conf` versus `redis.conf`), and their credentials often differ. NetBox therefore does not send the Redis server credentials to the Sentinel nodes unless instructed to. The following optional keys control how NetBox connects to the Sentinel nodes:

* `SENTINEL_AUTH`: Set to `True` to reuse `USERNAME` and `PASSWORD` when connecting to the Sentinel nodes (default: `False`). Leave this disabled if the Sentinel nodes do not require authentication: sending credentials to a Sentinel node with no password configured causes the connection to fail.
* `SENTINEL_USERNAME`: Username (ACL user) for the Sentinel nodes. Overrides `USERNAME` when `SENTINEL_AUTH` is enabled, and also works without it.
* `SENTINEL_PASSWORD`: Password for the Sentinel nodes. Overrides `PASSWORD` when `SENTINEL_AUTH` is enabled, and also works without it.
* `SENTINEL_KWARGS`: Optional dictionary of additional connection parameters for the Sentinel nodes, passed to redis-py as `sentinel_kwargs` (for example, to connect to the Sentinel nodes over TLS).

An empty `SENTINEL_USERNAME` or `SENTINEL_PASSWORD` is treated as unset. When the same parameter is set in more than one place, the order of precedence (highest first) is:

1. `SENTINEL_USERNAME` / `SENTINEL_PASSWORD`
2. `USERNAME` / `PASSWORD`, if `SENTINEL_AUTH` is enabled
3. `SENTINEL_TIMEOUT`, which is always applied as the Sentinel connection timeout (`socket_connect_timeout`)
4. `SENTINEL_KWARGS`
5. Any `socket_*` options in `KWARGS` (for example `socket_timeout` or `socket_keepalive`), which also apply to the Sentinel nodes, as they would with redis-py's own defaults

Example, with password-protected Sentinel nodes which are reached over TLS:

```python
REDIS = {
    'tasks': {
        'SENTINELS': [('mysentinel.redis.example.com', 26379)],
        'SENTINEL_SERVICE': 'netbox',
        'SENTINEL_PASSWORD': 'sentinel-secret',
        'SENTINEL_KWARGS': {
            'ssl': True,
            'ssl_ca_certs': '/etc/ssl/certs/ca.crt',
        },
        'PASSWORD': 'redis-secret',
        'DATABASE': 0,
    },
    'caching': {
        'SENTINELS': [('mysentinel.redis.example.com', 26379)],
        'SENTINEL_SERVICE': 'netbox',
        'SENTINEL_PASSWORD': 'sentinel-secret',
        'SENTINEL_KWARGS': {
            'ssl': True,
            'ssl_ca_certs': '/etc/ssl/certs/ca.crt',
        },
        'PASSWORD': 'redis-secret',
        'DATABASE': 1,
    }
}
```

If the Sentinel nodes share the Redis server's credentials, set `'SENTINEL_AUTH': True` in place of `SENTINEL_USERNAME` and `SENTINEL_PASSWORD`.

### SSL Configuration

If you need to configure SSL/TLS for Redis beyond the basic `SSL`, `CA_CERT_PATH`, and `INSECURE_SKIP_TLS_VERIFY` options (for example, client certificates, a specific TLS version, or custom ciphers), you can pass additional parameters via the `KWARGS` key in either the `tasks` or `caching` subsection.

NetBox already maps `CA_CERT_PATH` to `ssl_ca_certs` and (for caching) `INSECURE_SKIP_TLS_VERIFY` to `ssl_cert_reqs`; only add `KWARGS` when you need to override or extend those settings (for example, to supply client certificates or restrict TLS version or ciphers).

* `KWARGS` - Optional dictionary of additional SSL/TLS (or other) parameters passed to the Redis client. These are passed directly to the underlying Redis client: for `tasks` to [redis-py](https://redis-py.readthedocs.io/en/stable/connections.html), and for `caching` to the [django-redis](https://github.com/jazzband/django-redis#configure-as-cache-backend) connection pool.

When `tasks` uses Sentinel, `SSL`, `CA_CERT_PATH`, `INSECURE_SKIP_TLS_VERIFY` and `KWARGS` are passed as connection parameters for the Redis server which Sentinel points to; use `SENTINEL_KWARGS` for the Sentinel nodes themselves. When `tasks` uses a URL, `CA_CERT_PATH` and `KWARGS` are encoded in the URL, as described under [UNIX Socket Support](#unix-socket-support).

Example:

```python
REDIS = {
    'tasks': {
        'HOST': 'redis.example.com',
        'PORT': 1234,
        'SSL': True,
        'CA_CERT_PATH': '/etc/ssl/certs/ca.crt',
        'KWARGS': {
            'ssl_certfile': '/path/to/client-cert.pem',
            'ssl_keyfile': '/path/to/client-key.pem',
            'ssl_min_version': ssl.TLSVersion.TLSv1_2,
            'ssl_ciphers': 'HIGH:!aNULL',
        },
    },
    'caching': {
        'HOST': 'redis.example.com',
        'PORT': 1234,
        'SSL': True,
        'CA_CERT_PATH': '/etc/ssl/certs/ca.crt',
        'KWARGS': {
            'ssl_certfile': '/path/to/client-cert.pem',
            'ssl_keyfile': '/path/to/client-key.pem',
            'ssl_min_version': ssl.TLSVersion.TLSv1_2,
            'ssl_ciphers': 'HIGH:!aNULL',
        },
    }
}
```

!!! note
    If you use `ssl.TLSVersion` in your configuration (e.g. `ssl_min_version`), add `import ssl` at the top of your configuration file.

---

## SECRET_KEY

This is a secret, pseudorandom string used to assist in the creation new cryptographic hashes for passwords and HTTP cookies. The key defined here should not be shared outside the configuration file. `SECRET_KEY` can be changed at any time without impacting stored data, however be aware that doing so will invalidate all existing user sessions. NetBox deployments comprising multiple nodes must have the same secret key configured on all nodes.

`SECRET_KEY` **must** be at least 50 characters in length, and should contain a mix of letters, digits, and symbols. The script located at `$INSTALL_ROOT/netbox/generate_secret_key.py` may be used to generate a suitable key. For a Python package installation, run the virtual environment's `netbox secret-key` command instead. Please note that this key is **not** used directly for hashing user passwords or for the encrypted storage of secret data in NetBox.
