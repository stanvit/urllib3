# urllib3/poolmanager.py
# Copyright 2008-2012 Andrey Petrov and contributors (see CONTRIBUTORS.txt)
#
# This module is part of urllib3 and is released under
# the MIT License: http://www.opensource.org/licenses/mit-license.php

import logging

from ._collections import RecentlyUsedContainer
from .connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from .connectionpool import connection_from_url, port_by_scheme
from .request import RequestMethods
from .util import parse_url


__all__ = ['PoolManager', 'ProxyManager', 'proxy_from_url']


pool_classes_by_scheme = {
    'http': HTTPConnectionPool,
    'https': HTTPSConnectionPool,
}

log = logging.getLogger(__name__)


class PoolManager(RequestMethods):
    """
    Allows for arbitrary requests while transparently keeping track of
    necessary connection pools for you.

    :param num_pools:
        Number of connection pools to cache before discarding the least
        recently used pool.

    :param headers:
        Headers to include with all requests, unless other headers are given
        explicitly.

    :param \**connection_pool_kw:
        Additional parameters are used to create fresh
        :class:`urllib3.connectionpool.ConnectionPool` instances.

    Example: ::

        >>> manager = PoolManager(num_pools=2)
        >>> r = manager.request('GET', 'http://google.com/')
        >>> r = manager.request('GET', 'http://google.com/mail')
        >>> r = manager.request('GET', 'http://yahoo.com/')
        >>> len(manager.pools)
        2

    """

    def __init__(self, num_pools=10, headers=None, **connection_pool_kw):
        RequestMethods.__init__(self, headers)
        self.connection_pool_kw = connection_pool_kw
        self.pools = RecentlyUsedContainer(num_pools,
                                           dispose_func=lambda p: p.close())

    def clear(self):
        """
        Empty our store of pools and direct them all to close.

        This will not affect in-flight connections, but they will not be
        re-used after completion.
        """
        self.pools.clear()

    def connection_from_host(self, host, port=None, scheme='http'):
        """
        Get a :class:`ConnectionPool` based on the host, port, and scheme.

        If ``port`` isn't given, it will be derived from the ``scheme`` using
        ``urllib3.connectionpool.port_by_scheme``.
        """
        port = port or port_by_scheme.get(scheme, 80)

        pool_key = (scheme, host, port)

        # If the scheme, host, or port doesn't match existing open connections,
        # open a new ConnectionPool.
        pool = self.pools.get(pool_key)
        if pool:
            return pool

        # Make a fresh ConnectionPool of the desired type
        pool_cls = pool_classes_by_scheme[scheme]
        pool = pool_cls(host, port, **self.connection_pool_kw)

        self.pools[pool_key] = pool

        return pool

    def connection_from_url(self, url):
        """
        Similar to :func:`urllib3.connectionpool.connection_from_url` but
        doesn't pass any additional parameters to the
        :class:`urllib3.connectionpool.ConnectionPool` constructor.

        Additional parameters are taken from the :class:`.PoolManager`
        constructor.
        """
        u = parse_url(url)
        return self.connection_from_host(u.host, port=u.port, scheme=u.scheme)

    def urlopen(self, method, url, redirect=True, **kw):
        """
        Same as :meth:`urllib3.connectionpool.HTTPConnectionPool.urlopen`
        with custom cross-host redirect logic and only sends the request-uri
        portion of the ``url``.

        The given ``url`` parameter must be absolute, such that an appropriate
        :class:`urllib3.connectionpool.ConnectionPool` can be chosen for it.
        """
        u = parse_url(url)
        conn = self.connection_from_host(u.host, port=u.port, scheme=u.scheme)

        kw['assert_same_host'] = False
        kw['redirect'] = False
        if 'headers' not in kw:
            kw['headers'] = self.headers

        if getattr(self, 'proxy', None) and u.scheme == "http":
            response = conn.urlopen(method, url, **kw)
        else:
            response = conn.urlopen(method, u.request_uri, **kw)

        redirect_location = redirect and response.get_redirect_location()
        if not redirect_location:
            return response

        if response.status == 303:
            method = 'GET'

        log.info("Redirecting %s -> %s" % (url, redirect_location))
        kw['retries'] = kw.get('retries', 3) - 1  # Persist retries countdown
        return self.urlopen(method, redirect_location, **kw)

class ProxyManager(PoolManager):
    """
    Behaves just like PoolManager, but sends all requests through
    the defined proxy, using CONNECT method for HTTPS URLs
    """

    def __init__(self, proxy_url, num_pools=10, headers=None, proxy_headers=None, **connection_pool_kw):
        self.proxy = parse_url(proxy_url)
        self.proxy_headers = proxy_headers
        if self.proxy.scheme != "http":
            raise AssertionError('Not supported proxy scheme %s'%self.proxy.scheme)
        super(ProxyManager, self).__init__(num_pools, headers, **connection_pool_kw)

    def connection_from_host(self, host, port=None, scheme='http'):
        if scheme == "https":
            pool = super(ProxyManager,self).connection_from_host(host, port, scheme)
            pool.proxy = self.proxy
            pool.proxy_headers = self.proxy_headers
            return pool
        return super(ProxyManager,self).connection_from_host(self.proxy.host,
                self.proxy.port, self.proxy.scheme)

    def _set_proxy_headers(self, headers=None):
        headers_ = {'Accept': '*/*'}
        if headers:
            headers_.update(headers)

        return headers_

    def urlopen(self, method, url, redirect=True, **kw):
        "Same as HTTP(S)ConnectionPool.urlopen, ``url`` must be absolute."
        u = parse_url(url)

        if u.scheme == "http":
            # It's too late to set proxy headers on per-request basis for
            # tunnelled HTTPS connections, should use
            # constructor's proxy_headers instead
            kw['headers'] = self._set_proxy_headers(kw.get('headers'))
            if self.proxy_headers:
                kw['headers'].update(self.proxy_headers)

        return super(ProxyManager,self).urlopen(method, url, redirect, **kw)

def proxy_from_url(url, **kw):
    return ProxyManager(proxy_url=url, **kw)

