#!/usr/bin/env python

# dispyscheduler: Schedule jobs to nodes running 'dispynode';
# needed when multiple processes may use same nodes simultaneously
# in which case SharedJobCluster should be used;
# see accompanying 'dispy' for more details.

# Copyright (C) 2011 Giridhar Pemmasani (pgiri@yahoo.com)

# This file is part of dispy.

# dispy is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# dispy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.

# You should have received a copy of the GNU Lesser General Public License
# along with dispy.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import time
import socket
import stat
import cPickle
import threading
import struct
import base64
import logging
import re
import ssl
import hashlib
import atexit
import traceback
import Queue

from dispy import _Compute, DispyJob, _DispyJob_, _Node, _JobReply, \
     _xor_string, _parse_nodes, _node_name_ipaddr, _XferFile, _dispy_version

from asyncoro import Coro, AsynCoro, CoroCondition, AsynCoroSocket, MetaSingleton

from dispynode import _same_file

MaxFileSize = 10240000

class _Cluster(object):
    """Internal use only.
    """
    def __init__(self, compute):
        self._compute = compute
        compute.node_spec = _parse_nodes(compute.node_spec)
        logging.debug('node_spec: %s', str(compute.node_spec))
        self._pending_jobs = 0
        self._jobs = []
        self.cpu_time = 0
        self.start_time = time.time()
        self.end_time = None
        self.zombie = False
        self.last_pulse = time.time()
        self.client_job_result_port = None
        self.client_scheduler_ip_addr = None
        self.client_scheduler_port = None
        self.pending_results = 0

class _Scheduler(object):
    """Internal use only.

    See dispy's JobCluster and SharedJobCluster for documentation.
    """
    __metaclass__ = MetaSingleton

    def __init__(self, loglevel, nodes=[], ip_addr=None, port=None, node_port=None,
                 scheduler_port=0, pulse_interval=None, ping_interval=None,
                 node_secret='', node_keyfile=None, node_certfile=None,
                 cluster_secret='', cluster_keyfile=None, cluster_certfile=None,
                 dest_path_prefix=None, max_file_size=None, zombie_interval=60):
        if not hasattr(self, 'ip_addr'):
            atexit.register(self.shutdown)
            if not loglevel:
                loglevel = logging.WARNING
            logging.basicConfig(format='%(asctime)s %(message)s', level=loglevel)
            if ip_addr:
                ip_addr = _node_name_ipaddr(ip_addr)[1]
            else:
                ip_addr = socket.gethostbyname(socket.gethostname())
            if port is None:
                port = 51347
            if not node_port:
                node_port = 51348
            if not nodes:
                nodes = ['*']

            self.ip_addr = ip_addr
            self.port = port
            self.node_port = node_port
            self.scheduler_port = scheduler_port
            self.node_spec = nodes
            self._nodes = {}
            self.node_secret = node_secret
            self.node_keyfile = node_keyfile
            self.node_certfile = node_certfile
            self.cluster_secret = cluster_secret
            self.cluster_keyfile = cluster_keyfile
            self.cluster_certfile = cluster_certfile
            if not dest_path_prefix:
                dest_path_prefix = os.path.join(os.sep, 'tmp', 'dispyscheduler')
            self.dest_path_prefix = dest_path_prefix
            if not os.path.isdir(self.dest_path_prefix):
                os.makedirs(self.dest_path_prefix)
                os.chmod(self.dest_path_prefix, stat.S_IWUSR | stat.S_IXUSR)
            if max_file_size is None:
                max_file_size = MaxFileSize
            self.max_file_size = max_file_size
            if zombie_interval:
                self.zombie_interval = 60 * zombie_interval
            else:
                self.zombie_interval = None

            if pulse_interval:
                try:
                    self.pulse_interval = float(pulse_interval)
                    assert 1.0 <= self.pulse_interval <= 1000
                except:
                    raise Exception('Invalid pulse_interval; must be between 1 and 1000')
            else:
                self.pulse_interval = None

            if ping_interval:
                try:
                    self.ping_interval = float(ping_interval)
                    assert 1.0 <= self.ping_interval <= 1000
                except:
                    raise Exception('Invalid ping_interval; must be between 1 and 1000')
            else:
                self.ping_interval = None

            self.asyncoro = AsynCoro()

            self._clusters = {}
            self.cluster_id = 1
            self.unsched_jobs = 0
            self.job_uid = 1
            self._sched_jobs = {}
            self._sched_cv = CoroCondition()
            self._terminate_scheduler = False
            self.sign = os.urandom(20).encode('hex')
            self.auth_code = hashlib.sha1(_xor_string(self.sign, self.cluster_secret)).hexdigest()
            logging.debug('auth_code: %s', self.auth_code)

            #self.select_job_node = self.fast_node_schedule
            self.select_job_node = self.load_balance_schedule
            self.start_time = time.time()

            self.timer_coro = Coro(self.timer_task)

            self.request_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.request_sock.bind((self.ip_addr, self.scheduler_port))
            self.scheduler_port = self.request_sock.getsockname()[1]
            self.request_sock.listen(32)
            self.request_sock = AsynCoroSocket(self.request_sock, blocking=False, timeout=False)
            self.request_coro = Coro(self.request_server)

            self.job_result_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.job_result_sock = AsynCoroSocket(self.job_result_sock,
                                                  blocking=False, timeout=False)
            self.job_result_sock.bind((self.ip_addr, 0))
            self.job_result_sock.listen(50)
            self.job_result_coro = Coro(self.job_result_server)

            self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.udp_sock.bind(('', self.port))
            self.udp_sock = AsynCoroSocket(self.udp_sock, blocking=False, timeout=False)
            logging.debug('UDP server at %s:%s' % (self.udp_sock.getsockname()))
            self.udp_coro = Coro(self.udp_server)

            logging.debug('TCP port is %s:%s', self.ip_addr, self.scheduler_port)
            self.scheduler_coro = Coro(self._schedule_jobs)

            # TODO: wait for scheduler and udp servers to start
            bc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            bc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

            ping_request = cPickle.dumps({'scheduler_ip_addr':self.ip_addr,
                                          'scheduler_port':self.port,
                                          'version':_dispy_version})
            node_spec = _parse_nodes(nodes)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for node_spec, node_info in node_spec.iteritems():
                logging.debug('Node: %s, %s', node_spec, str(node_info))
                # TODO: broadcast only if node_spec is wildcard that
                # matches local network and only in that case, or if
                # node_spec is '.*'
                if node_spec.find('*') >= 0:
                    port = node_info['port']
                    if not port:
                        port = self.node_port
                    logging.debug('Broadcasting to %s', port)
                    bc_sock.sendto('PING:%s' % ping_request, ('<broadcast>', port))
                    continue
                ip_addr = node_info['ip_addr']
                port = node_info['port']
                if not port:
                    port = self.node_port
                sock.sendto('PING:%s' % ping_request, (ip_addr, port))
            bc_sock.close()
            sock.close()

    def udp_server(self, coro=None):
        # generator
        assert coro is not None

        while True:
            msg, addr = yield self.udp_sock.recvfrom(1024)
            # no need to create coros to process these requests
            if msg.startswith('PULSE:'):
                msg = msg[len('PULSE:'):]
                try:
                    info = cPickle.loads(msg)
                except:
                    logging.warning('Ignoring pulse message from %s', addr[0])
                    continue
                if 'client_scheduler_ip_addr' in info:
                    self._sched_cv.acquire()
                    for cluster in self._clusters.itervalues():
                        if cluster.client_scheduler_ip_addr == info['client_scheduler_ip_addr'] and \
                               cluster.client_scheduler_port == info['client_scheduler_port']:
                            cluster.last_pulse = time.time()
                    yield self._sched_cv.release()
                else:
                    node = self._nodes.get(info['ip_addr'], None)
                    if node is not None:
                        # assert 0 <= info['cpus'] <= node.cpus
                        node.last_pulse = time.time()
                        msg = 'PULSE:' + cPickle.dumps({'ip_addr':self.ip_addr})
                        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        sock = AsynCoroSocket(sock, blocking=False)
                        yield sock.sendto(msg, (info['ip_addr'], info['port']))
                        sock.close()
            elif msg.startswith('PONG:'):
                try:
                    status = cPickle.loads(msg[len('PONG:'):])
                    assert status['port'] > 0 and status['cpus'] > 0
                    assert status['version'] == _dispy_version
                except:
                    logging.debug('Ignoring node %s', addr[0])
                    continue
                logging.debug('Discovered %s:%s with %s cpus',
                              status['ip_addr'], status['port'], status['cpus'])
                if status['cpus'] <= 0:
                    logging.debug('Ignoring node %s', status['ip_addr'])
                    continue
                self._sched_cv.acquire()
                node = self._nodes.get(status['ip_addr'], None)
                if node is None:
                    node = _Node(status['ip_addr'], status['port'], status['cpus'],
                                 status['sign'], self.node_secret,
                                 self.node_keyfile, self.node_certfile)
                    self._nodes[node.ip_addr] = node
                else:
                    node.last_pulse = time.time()
                    h = _xor_string(status['sign'], self.node_secret)
                    h = hashlib.sha1(h).hexdigest()
                    if node.port == status['port'] and node.auth_code == h:
                        yield self._sched_cv.release()
                        logging.debug('Node %s is already known', node.ip_addr)
                        continue
                    node.port = status['port']
                    node.auth_code = h
                node_computes = []
                for cid, cluster in self._clusters.iteritems():
                    compute = cluster._compute
                    if node.ip_addr in compute.nodes:
                        continue
                    for node_spec, host in compute.node_spec.iteritems():
                        if re.match(node_spec, node.ip_addr):
                            if host['name'] is None or host['name'].find('*') >= 0:
                                node.name = node.ip_addr
                            else:
                                node.name = host['name']
                            node_computes.append(compute)
                            break
                if node_computes:
                    Coro(self.setup_node, node, node_computes)
                yield self._sched_cv.release()
            elif msg.startswith('TERMINATED:'):
                try:
                    data = cPickle.loads(msg[len('TERMINATED:'):])
                    self._sched_cv.acquire()
                    node = self._nodes.pop(data['ip_addr'], None)
                    if not node:
                        yield self._sched_cv.release()
                        continue
                    logging.debug('Removing node %s', node.ip_addr)
                    h = _xor_string(data['sign'], self.node_secret)
                    auth_code = hashlib.sha1(h).hexdigest()
                    if auth_code != node.auth_code:
                        logging.warning('Invalid signature from %s', node.ip_addr)
                    dead_jobs = [_job for _job in self._sched_jobs.itervalues() \
                                 if _job.node is not None and \
                                 _job.node.ip_addr == node.ip_addr]
                    self.reschedule_jobs(dead_jobs)
                    for cid, cluster in self._clusters.iteritems():
                        if cluster._compute.nodes.pop(node.ip_addr, None) is not None:
                            try:
                                node.clusters.remove(cid)
                            except:
                                logging.debug('Cluster %s/%s is already removed from %s',
                                              cluster.compute.name, cluster.compute.id,
                                              node.ip_addr)
                                pass
                    self._sched_cv.notify()
                    yield self._sched_cv.release()
                    del node
                except:
                    logging.debug('Removing node failed: %s', traceback.format_exc())
            elif msg.startswith('SERVERPORT:'):
                try:
                    req = cPickle.loads(msg[len('SERVERPORT:'):])
                    logging.debug('Sending %s:%s to %s:%s',
                                  self.ip_addr, self.scheduler_port,
                                  req['ip_addr'], req['port'])
                    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    sock = AsynCoroSocket(sock, blocking=False)
                    reply = {'ip_addr':self.ip_addr, 'port':self.scheduler_port,
                             'sign':self.sign, 'version':_dispy_version}
                    yield sock.sendto(cPickle.dumps(reply), (req['ip_addr'], req['port']))
                    sock.close()
                except:
                    logging.debug(traceback.format_exc())
                    # pass
            else:
                logging.debug('Ignoring UDP message %s from: %s',
                              msg[:min(5, len(msg))], addr[0])

    def send_ping_cluster(self, cluster, coro=None):
        # generator
        ping_request = cPickle.dumps({'scheduler_ip_addr':self.ip_addr,
                                      'scheduler_port':self.port, 'version':_dispy_version})
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock = AsynCoroSocket(sock, blocking=False)
        for node_spec, node_info in cluster._compute.node_spec.iteritems():
            if node_spec.find('*') >= 0:
                port = node_info['port']
                if not port:
                    port = self.node_port
                bc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                bc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                bc_osck = AsynCoroSocket(bc_sock, blocking=False)
                try:
                    yield bc_sock.sendto('PING:%s' % ping_request, ('<broadcast>', port))
                except:
                    pass
                bc_sock.close()
            else:
                ip_addr = node_info['ip_addr']
                if ip_addr in cluster._compute.nodes:
                    continue
                port = node_info['port']
                if not port:
                    port = self.node_port
                try:
                    yield sock.sendto('PING:%s' % ping_request, (ip_addr, port))
                except:
                    pass
        sock.close()

    def add_cluster(self, cluster, coro=None):
        # generator
        assert coro is not None
        compute = cluster._compute
        compute.pulse_interval = self.pulse_interval
        # TODO: should we allow clients to add new nodes, or use only
        # the nodes initially created with command-line?
        yield self.send_ping_cluster(cluster, coro)
        self._sched_cv.acquire()
        compute_nodes = []
        for node_spec, host in compute.node_spec.iteritems():
            for ip_addr, node in self._nodes.iteritems():
                if ip_addr in compute.nodes:
                    continue
                if re.match(node_spec, ip_addr):
                    compute_nodes.append(node)
                    break
        # self._sched_cv.notify()
        yield self._sched_cv.release()

        for node in compute_nodes:
            r = yield node.setup(compute, coro=coro)
            if r:
                logging.warning('Failed to setup %s for computation "%s"',
                                node.ip_addr, compute.name)
            else:
                self._sched_cv.acquire()
                if node.ip_addr not in compute.nodes:
                    compute.nodes[node.ip_addr] = node
                    node.clusters.append(compute.id)
                    self._sched_cv.notify()
                self._sched_cv.release()

    def setup_node(self, node, computes, coro=None):
        # generator
        assert coro is not None
        for compute in computes:
            r = yield node.setup(compute, coro=coro)
            if r:
                logging.warning('Failed to setup %s for computation "%s"',
                                node.ip_addr, compute.name)
            else:
                self._sched_cv.acquire()
                if node.ip_addr not in compute.nodes:
                    compute.nodes[node.ip_addr] = node
                    node.clusters.append(compute.id)
                    self._sched_cv.notify()
                yield self._sched_cv.release()

    def send_job_result(self, uid, cid, ip, port, result, coro=None):
        # generator
        assert coro is not None
        logging.debug('Sending results for %s to %s, %s', uid, ip, port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock = AsynCoroSocket(sock, blocking=False)
        try:
            yield sock.connect((ip, port))
            yield sock.write_msg(uid, cPickle.dumps(result))
            ruid, ack = yield sock.read_msg()
            assert ack == 'ACK'
            assert uid == ruid
        except:
            f = os.path.join(self.dest_path_prefix, str(cid), '_dispy_job_reply_%s' % uid)
            logging.debug('storing results for job %s', uid)
            try:
                # TODO: file operations should be done asynchronously with coros
                fd = open(f, 'wb')
                cPickle.dump(result, fd)
                fd.close()
            except:
                logging.debug('Could not save results for job %s', uid)
                logging.debug(traceback.format_exc())

            self._sched_cv.acquire()
            cluster = self._clusters.get(cid, None)
            if cluster:
                cluster.pending_results += 1
            self._sched_cv.release()
        else:
            cluster = self._clusters.get(cid, None)
            if cluster:
                cluster.last_pulse = time.time()
        finally:
            sock.close()

    def reschedule_jobs(self, dead_jobs):
        # non-generator called with _sched_cv locked
        for _job in dead_jobs:
            cluster = self._clusters[_job.compute_id]
            del self._sched_jobs[_job.uid]
            if cluster._compute.resubmit:
                logging.debug('Rescheduling job %s from %s',
                              _job.uid, _job.node.ip_addr)
                _job.job.status = DispyJob.Created
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
            else:
                logging.debug('Terminating job %s scheduled on %s',
                              _job.uid, _job.node.ip_addr)
                reply = _JobReply(_job, _job.node.ip_addr, status=DispyJob.Terminated)
                cluster._pending_jobs -= 1
                if cluster._pending_jobs == 0:
                    cluster.end_time = time.time()
                Coro(self.send_job_result, _job.uid, cluster._compute.id,
                     cluster.client_scheduler_ip_addr, cluster.client_job_result_port, reply)

    def request_server(self, coro=None):
        # generator
        assert coro is not None

        def _request_task(self, conn, addr, coro=None):

            def _job_request_task(self, conn, msg, addr, coro):
                # generator
                try:
                    _job = cPickle.loads(msg)
                except:
                    logging.debug('Ignoring job request from %s', addr[0])
                    raise StopIteration
                self._sched_cv.acquire()
                _job.uid = self.job_uid
                self.job_uid += 1
                if self.job_uid == sys.maxint:
                    # TODO: check if it is okay to reset
                    self.job_uid = 1
                yield self._sched_cv.release()
                setattr(_job, 'node', None)
                job = type('DispyJob', (), {'status':DispyJob.Created,
                                            'start_time':None, 'end_time':None})
                setattr(_job, 'job', job)
                resp = cPickle.dumps(_job.uid)
                try:
                    yield conn.write_msg(0, resp)
                except:
                    logging.warning('Failed to send response to %s: %s',
                                    str(addr), traceback.format_exc())
                    raise StopIteration
                try:
                    uid, msg = yield conn.read_msg()
                    assert msg == 'ACK'
                    assert uid == _job.uid
                except:
                    logging.warning('Invalid reply for job: %s, %s',
                                    _job.uid, traceback.format_exc())
                    raise StopIteration
                self._sched_cv.acquire()
                cluster = self._clusters.get(_job.compute_id, None)
                if cluster is None:
                    logging.debug('cluster %s is not valid anymore for job %s',
                                  _job.compute_id, _job.uid)
                    self._sched_cv.release()
                    raise StopIteration
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
                cluster._pending_jobs += 1
                cluster.last_pulse = time.time()
                self._sched_cv.notify()
                self._sched_cv.release()

            def _compute_task(self, conn, msg, addr, coro):
                # generator
                try:
                    compute = cPickle.loads(msg)
                except:
                    logging.debug('Ignoring compute request from %s', addr[0])
                    yield None; raise StopIteration
                setattr(compute, 'nodes', {})
                cluster = _Cluster(compute)
                compute = cluster._compute
                cluster.client_job_result_port = compute.job_result_port
                cluster.client_scheduler_ip_addr = compute.scheduler_ip_addr
                cluster.client_scheduler_port = compute.scheduler_port
                compute.job_result_port = self.job_result_sock.getsockname()[1]
                compute.scheduler_ip_addr = self.ip_addr
                compute.scheduler_port = self.port
                self._sched_cv.acquire()
                compute.id = cluster.id = self.cluster_id
                self._clusters[cluster.id] = cluster
                self.cluster_id += 1
                dest_path = os.path.join(self.dest_path_prefix, str(compute.id))
                if not os.path.isdir(dest_path):
                    try:
                        os.makedirs(dest_path)
                    except:
                        logging.warning('Could not create directory "%s"', dest_path)
                        if compute.xfer_files:
                            self._sched_cv.release()
                            conn.close()
                            raise StopIteration
                for xf in compute.xfer_files:
                    xf.compute_id = compute.id
                    xf.name = os.path.join(dest_path, os.path.basename(xf.name))
                self._sched_cv.release()
                resp = {'ID':compute.id,
                        'pulse_interval':(self.zombie_interval / 5.0) if self.zombie_interval else None}
                resp = cPickle.dumps(resp)
                logging.debug('New computation %s: %s, %s', compute.id, compute.name,
                              len(compute.xfer_files))
                yield resp

            def _xfer_file_task(self, conn, msg, addr, coro):
                # generator
                try:
                    xf = cPickle.loads(msg)
                except:
                    logging.debug('Ignoring file trasnfer request from %s', addr[0])
                    raise StopIteration
                resp = ''
                if xf.compute_id not in self._clusters:
                    logging.error('computation "%s" is invalid' % xf.compute_id)
                    raise StopIteration
                compute = self._clusters[xf.compute_id]
                dest_path = os.path.join(self.dest_path_prefix, str(compute.id))
                if not os.path.isdir(dest_path):
                    try:
                        os.makedirs(dest_path)
                    except:
                        raise StopIteration
                tgt = os.path.join(dest_path, os.path.basename(xf.name))
                logging.debug('Copying file %s to %s (%s)', xf.name,
                              tgt, xf.stat_buf.st_size)
                try:
                    fd = open(tgt, 'wb')
                    n = 0
                    while n < xf.stat_buf.st_size:
                        data = yield conn.read(min(xf.stat_buf.st_size-n, 1024000))
                        if not data:
                            break
                        fd.write(data)
                        n += len(data)
                        if self.max_file_size and n > self.max_file_size:
                            logging.warning('File "%s" is too big (%s); it is truncated',
                                            tgt, n)
                            break
                    fd.close()
                    if n < xf.stat_buf.st_size:
                        resp = 'NAK (read only %s bytes)' % n
                    else:
                        resp = 'ACK'
                        logging.debug('Copied file %s, %s', tgt, resp)
                        os.utime(tgt, (xf.stat_buf.st_atime, xf.stat_buf.st_mtime))
                        os.chmod(tgt, stat.S_IMODE(xf.stat_buf.st_mode))
                except:
                    logging.warning('Copying file "%s" failed with "%s"',
                                    xf.name, traceback.format_exc())
                    resp = 'NACK'
                if resp:
                    try:
                        yield conn.write_msg(0, resp)
                    except:
                        logging.debug('Could not send reply for "%s"', xf.name)

            # _request_task begins here
            conn = AsynCoroSocket(conn, blocking=False, certfile=self.cluster_certfile,
                                  keyfile=self.cluster_keyfile, server=True)

            resp = None
            try:
                req = yield conn.read(len(self.auth_code))
                if req != self.auth_code:
                    conn.close()
                    logging.warning('Invalid/unauthorized request ignored')
                    raise StopIteration
                uid, msg = yield conn.read_msg()
                if not msg:
                    conn.close()
                    logging.info('Closing connection')
                    raise StopIteration
            except:
                logging.warning('Failed to read message from %s: %s',
                                str(addr), traceback.format_exc())
                conn.close()
                raise StopIteration
            if msg.startswith('JOB:'):
                msg = msg[len('JOB:'):]
                yield _job_request_task(self, conn, msg, addr, coro)
                conn.close()
            elif msg.startswith('COMPUTE:'):
                msg = msg[len('COMPUTE:'):]
                resp = yield _compute_task(self, conn, msg, addr, coro)
            elif msg.startswith('ADD_COMPUTE:'):
                msg = msg[len('ADD_COMPUTE:'):]
                self._sched_cv.acquire()
                try:
                    req = cPickle.loads(msg)
                    cluster = self._clusters[req['ID']]
                    for xf in cluster._compute.xfer_files:
                        assert os.path.isfile(xf.name)
                    resp = cPickle.dumps(cluster._compute.id)
                    Coro(self.add_cluster, cluster)
                except:
                    logging.debug('Ignoring compute request from %s', addr[0])
                    conn.close()
                    raise StopIteration
                finally:
                    yield self._sched_cv.release()
            elif msg.startswith('DEL_COMPUTE:'):
                msg = msg[len('DEL_COMPUTE:'):]
                conn.close()
                try:
                    req = cPickle.loads(msg)
                    assert isinstance(req['ID'], int)
                except:
                    logging.warning('Invalid compuation for deleting')
                    raise StopIteration
                self._sched_cv.acquire()
                cluster = self._clusters.get(req['ID'], None)
                if cluster is None:
                    # this cluster is closed
                    self._sched_cv.release()
                    raise StopIteration
                cluster.zombie = True
                self.cleanup_computation(cluster)
                yield self._sched_cv.release()
            elif msg.startswith('FILEXFER:'):
                msg = msg[len('FILEXFER:'):]
                yield _xfer_file_task(self, conn, msg, addr, coro)
            elif msg.startswith('TERMINATE_JOB:'):
                msg = msg[len('TERMINATE_JOB:'):]
                conn.close()
                try:
                    job = cPickle.loads(msg)
                except:
                    logging.warning('Invalid job cancel message')
                    raise StopIteration
                self._sched_cv.acquire()
                cluster = self._clusters.get(job.compute_id, None)
                if not cluster:
                    logging.debug('Invalid job %s!', job.uid)
                    self._sched_cv.release()
                    raise StopIteration
                compute = cluster._compute
                cluster.last_pulse = time.time()
                _job = self._sched_jobs.get(job.uid, None)
                if _job is None:
                    for i, _job in enumerate(cluster._jobs):
                        if _job.uid == job.uid:
                            del cluster._jobs[i]
                            self.unsched_jobs -= 1
                            reply = _JobReply(_job, self.ip_addr, status=DispyJob.Cancelled)
                            Coro(self.send_job_result,
                                 _job.uid, compute.id, cluster.client_scheduler_ip_addr,
                                 cluster.client_job_result_port, reply)
                            break
                    else:
                        logging.debug('Invalid job %s!', job.uid)
                else:
                    _job.job.status = DispyJob.Cancelled
                    Coro(_job.node.send, _job.uid, 'TERMINATE_JOB:' + cPickle.dumps(_job),
                         reply=False)
                yield self._sched_cv.release()
            elif msg.startswith('RETRIEVE_JOB:'):
                req = msg[len('RETRIEVE_JOB:'):]
                try:
                    req = cPickle.loads(req)
                    assert req['uid'] is not None
                    assert req['hash'] is not None
                    assert req['compute_id'] is not None
                    result_file = os.path.join(self.dest_path_prefix, str(req['compute_id']),
                                               '_dispy_job_reply_%s' % req['uid'])
                    if os.path.isfile(result_file):
                        fd = open(result_file, 'rb')
                        job_reply = cPickle.load(fd)
                        fd.close()
                        if job_reply.hash == req['hash']:
                            yield conn.write_msg(req['uid'], cPickle.dumps(job_reply))
                            ruid, ack = yield conn.read_msg()
                            assert ack == 'ACK'
                            assert ruid == req['uid']
                            try:
                                os.remove(result_file)
                                self._sched_cv.acquire()
                                cluster = self._clusters.get(req['compute_id'], None)
                                if cluster is None:
                                    p = os.path.dirname(result_file)
                                    if len(os.listdir(p)) == 0:
                                        os.rmdir(p)
                                else:
                                    cluster.pending_results -= 1
                                yield self._sched_cv.release()
                            except:
                                logging.debug('Could not remove "%s"', result_file)
                            raise StopIteration
                        else:
                            resp = cPickle.dumps('Invalid job')
                except:
                    resp = cPickle.dumps('Invalid job')
                    # logging.debug(traceback.format_exc())
            else:
                logging.debug('Ignoring invalid command')

            if resp is not None:
                try:
                    yield conn.write_msg(0, resp)
                except:
                    logging.warning('Failed to send response to %s: %s',
                                    str(addr), traceback.format_exc())
                conn.close()
            # end of _request_task

        # request_server starts here
        logging.info('scheduler running at %s:%s', self.ip_addr, self.port)
        while True:
            conn, addr = yield self.request_sock.accept()
            Coro(_request_task, self, conn, addr)

    def timer_task(self, coro=None):
        reset = True
        last_ping_time = last_pulse_time = last_zombie_time = time.time()
        while True:
            if reset:
                if self.pulse_interval:
                    self.pulse_timeout = 5.0 * self.pulse_interval
                else:
                    self.pulse_timeout = None

                if self.pulse_timeout and self.ping_interval:
                    timeout = min(self.pulse_timeout, self.ping_interval)
                else:
                    timeout = max(self.pulse_timeout, self.ping_interval)

                if timeout and self.zombie_interval:
                    timeout = min(timeout, self.zombie_interval)
                    self.zombie_interval = max(5 * timeout, self.zombie_interval)
                else:
                    timeout = max(timeout, self.zombie_interval)

            reset = yield coro.suspend(timeout)

            now = time.time()
            if self.pulse_timeout and (now - last_pulse_time) >= self.pulse_timeout:
                last_pulse_time = now
                self._sched_cv.acquire()
                dead_nodes = {}
                for node in self._nodes.itervalues():
                    if node.busy and node.last_pulse + pulse_timeout < now:
                        logging.warning('Node %s is not responding; removing it (%s, %s, %s)',
                                        node.ip_addr, node.busy, node.last_pulse, now)
                        dead_nodes[node.ip_addr] = node
                for ip_addr in dead_nodes:
                    del self._nodes[ip_addr]
                    for cluster in self._clusters.itervalues():
                        cluster._compute.nodes.pop(ip_addr, None)
                dead_jobs = [_job for _job in self._sched_jobs.itervalues() \
                             if _job.node is not None and _job.node.ip_addr in dead_nodes]
                self.reschedule_jobs(dead_jobs)
                if dead_nodes or dead_jobs:
                    self._sched_cv.notify()
                yield self._sched_cv.release()
            if self.ping_interval and (now - last_ping_time) >= self.ping_interval:
                last_ping_time = now
                self._sched_cv.acquire()
                for cluster in self.clusters.itervalues():
                    Coro(self.send_ping_cluster, cluster)
                yield self._sched_cv.release()
            if self.zombie_interval and (now - last_zombie_time) >= self.zombie_interval:
                last_zombie_time = now
                self._sched_cv.acquire()
                for cluster in self._clusters.itervalues():
                    if (now - cluster.last_pulse) > self.zombie_interval:
                        cluster.zombie = True
                zombies = [cluster for cluster in self._clusters.itervalues() \
                           if cluster.zombie and cluster._pending_jobs == 0]
                for cluster in zombies:
                    logging.debug('Deleting zombie computation "%s" / %s',
                                  cluster._compute.name, cluster._compute.id)
                    self.cleanup_computation(cluster)
                phoenix = [cluster for cluster in self._clusters.itervalues() \
                           if not cluster.zombie and cluster.pending_results]
                for cluster in phoenix:
                    compute = cluster._compute
                    result_dir = os.path.join(self.dest_path_prefix, str(compute.id))
                    files = [f for f in os.listdir(result_dir) \
                             if f.startswith('_dispy_job_reply_')]
                    # limit number queued so as not to take up too much time
                    files = files[:min(len(files), 128)]
                    for f in files:
                        result_file = os.path.join(result_dir, f)
                        try:
                            fd = open(result_file, 'rb')
                            result = cPickle.load(fd)
                            fd.close()
                            Coro(self.send_job_result,
                                 result.uid, compute.id, cluster.client_scheduler_ip_addr,
                                 cluster.client_job_result_port, result)
                        except:
                            logging.debug('Could not load "%s"', result_file)
                        else:
                            cluster.pending_results -= 1
                        try:
                            os.remove(result_file)
                        except:
                            logging.debug('Could not remove "%s"', result_file)
                yield self._sched_cv.release()

    def load_balance_schedule(self):
        node = None
        load = None
        for ip_addr, host in self._nodes.iteritems():
            if host.busy >= host.cpus:
                continue
            if all(not self._clusters[cluster_id]._jobs for cluster_id in host.clusters):
                continue
            logging.debug('load: %s, %s, %s' % (host.ip_addr, host.busy, host.cpus))
            if (load is None) or ((float(host.busy) / host.cpus) < load):
                node = host
                load = float(node.busy) / node.cpus
        return node

    def job_result_task(self, conn, addr, coro=None):
        # generator
        assert coro is not None
        conn = AsynCoroSocket(conn, blocking=False, certfile=self.node_certfile,
                              keyfile=self.node_keyfile, server=True)

        try:
            uid, msg = yield conn.read_msg()
            yield conn.write_msg(uid, 'ACK')
        except:
            logging.warning('Failed to read job results from %s', str(addr))
            raise StopIteration
        finally:
            conn.close()

        logging.debug('Received reply for job %s from %s' % (uid, addr[0]))
        self._sched_cv.acquire()
        node = self._nodes.get(addr[0], None)
        if node is None:
            self._sched_cv.release()
            logging.warning('Ignoring invalid reply for job %s from %s',
                            uid, addr[0])
            raise StopIteration
        node.last_pulse = time.time()
        _job = self._sched_jobs.get(uid, None)
        if _job is None:
            self._sched_cv.release()
            logging.warning('Ignoring invalid job %s from %s', uid, addr[0])
            raise StopIteration
        _job.job.end_time = time.time()
        cluster = self._clusters.get(_job.compute_id, None)
        if cluster is None:
            self._sched_cv.release()
            logging.warning('Invalid cluster for job %s from %s', uid, addr[0])
            raise StopIteration
        compute = cluster._compute
        try:
            reply = cPickle.loads(msg)
            assert reply.uid == _job.uid
            assert reply.hash == _job.hash
            assert _job.job.status not in [DispyJob.Created, DispyJob.Finished]
            setattr(reply, 'cpus', node.cpus)
            setattr(reply, 'start_time', _job.job.start_time)
            setattr(reply, 'end_time', _job.job.end_time)
            if reply.status == DispyJob.ProvisionalResult:
                logging.debug('Receveid provisional result for %s', uid)
                Coro(self.send_job_result,
                     _job.uid, compute.id, cluster.client_scheduler_ip_addr,
                     cluster.client_job_result_port, reply)
                self._sched_cv.release()
                raise StopIteration
            else:
                del self._sched_jobs[uid]
        except:
            self._sched_cv.release()
            logging.warning('Invalid job result for %s from %s', uid, addr[0])
            logging.debug(traceback.format_exc())
            raise StopIteration

        _job.node.busy -= 1
        assert compute.nodes[addr[0]] == _job.node
        if reply.status != DispyJob.ProvisionalResult:
            if reply.status != DispyJob.Terminated:
                _job.node.jobs += 1
            _job.node.cpu_time += _job.job.end_time - _job.job.start_time
            cluster._pending_jobs -= 1
            if cluster._pending_jobs == 0:
                cluster.end_time = time.time()
        Coro(self.send_job_result, _job.uid, compute.id, cluster.client_scheduler_ip_addr,
             cluster.client_job_result_port, reply)
        self._sched_cv.notify()
        self._sched_cv.release()

    def job_result_server(self, coro=None):
        # generator
        assert coro is not None
        logging.debug('Job results port is %s:%s',
                      self.ip_addr, self.job_result_sock.getsockname()[1])
        while True:
            conn, addr = yield self.job_result_sock.accept()
            if addr[0] not in self._nodes:
                logging.warning('Ignoring results from %s', addr[0])
                conn.close()
                continue
            Coro(self.job_result_task, conn, addr)

    def fast_node_schedule(self):
        # as we eagerly schedule, this has limited advantages
        # (useful only when  we have data about all the nodes and more than one node
        # is currently available)
        # in addition, we assume all jobs take equal time to execute
        node = None
        secs_per_job = None
        for ip_addr, host in self._nodes.iteritems():
            if host.busy >= host.cpus:
                continue
            if all(not self._clusters[cluster_id]._jobs for cluster_id in host.clusters):
                continue
            if (secs_per_job is None) or (host.jobs == 0) or \
                   ((host.cpu_time / host.jobs) <= secs_per_job):
                node = host
                if host.jobs == 0:
                    secs_per_job = 0
                else:
                    secs_per_job = host.cpu_time / host.jobs
        return node

    def run_job(self, _job, cluster, coro=None):
        # generator
        assert coro is not None
        try:
            yield _job.run(coro=coro)
        except EnvironmentError:
            logging.warning('Failed to run job %s on %s for computation %s; removing this node',
                            _job.uid, _job.node.ip_addr, cluster._compute.name)
            self._sched_cv.acquire()
            if cluster._compute.nodes.pop(_job.node.ip_addr, None) is not None:
                try:
                    _job.node.clusters.remove(cluster.compute.id)
                except:
                    logging.debug('Cluster %s/%s is already removed from %s',
                                  cluster.compute.name, cluster.compute.id, _job.node.ip_addr)
                    pass
                # TODO: remove the node from all clusters and globally?
            # this job might have been deleted already due to timeout
            if self._sched_jobs.pop(_job.uid, None) == _job:
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
                _job.node.busy -= 1
            self._sched_cv.notify()
            self._sched_cv.release()
        except:
            logging.debug(traceback.format_exc())
            logging.warning('Failed to run job %s on %s for computation %s; rescheduling it',
                            _job.uid, _job.node.ip_addr, cluster._compute.name)
            # TODO: delay executing again for some time?
            self._sched_cv.acquire()
            # this job might have been deleted already due to timeout
            if self._sched_jobs.pop(_job.uid, None) == _job:
                cluster._jobs.append(_job)
                self.unsched_jobs += 1
                _job.node.busy -= 1
            self._sched_cv.notify()
            self._sched_cv.release()

    def _schedule_jobs(self, coro=None):
        # generator
        assert coro is not None
        while True:
            self._sched_cv.acquire()
            # n = sum(len(cluster._jobs) for cluster in self._clusters.itervalues())
            # assert self.unsched_jobs == n, '%s != %s' % (self.unsched_jobs, n)
            while self._sched_cv.wait():
                yield None
            if self._terminate_scheduler:
                yield self._sched_cv.release()
                break
            while self.unsched_jobs:
                logging.debug('Pending jobs: %s', self.unsched_jobs)
                node = self.select_job_node()
                if node is None:
                    logging.debug('No nodes/jobs')
                    break
                # TODO: strategy to pick a cluster?
                for cid in node.clusters:
                    if self._clusters[cid]._jobs:
                        _job = self._clusters[cid]._jobs.pop(0)
                        break
                else:
                    break
                cluster = self._clusters[_job.compute_id]
                _job.node = node
                logging.debug('Scheduling job %s on %s (load: %.3f)',
                              _job.uid, node.ip_addr, float(node.busy) / node.cpus)
                assert node.busy < node.cpus
                # _job.ip_addr = node.ip_addr
                self._sched_jobs[_job.uid] = _job
                self.unsched_jobs -= 1
                node.busy += 1
                Coro(self.run_job, _job, cluster)
            yield self._sched_cv.release()
        self._sched_cv.acquire()
        logging.debug('scheduler quitting (%s / %s)',
                      len(self._sched_jobs), self.unsched_jobs)
        for cid, cluster in self._clusters.iteritems():
            compute = cluster._compute
            for node in compute.nodes.itervalues():
                node.close(compute)
            for _job in cluster._jobs:
                reply = _JobReply(_job, self.ip_addr, status=DispyJob.Terminated)
                Coro(self.send_job_result, _job.uid, compute.id, cluster.client_scheduler_ip_addr,
                     cluster.client_job_result_port, reply)
            cluster._jobs = []
        yield self._sched_cv.release()
        logging.debug('scheduler quit')

    def shutdown(self):
        def _shutdown(self, coro=None):
            # generator
            assert coro is not None
            # TODO: send shutdown notification to clients? Or wait for all
            # pending tasks to complete?
            self._sched_cv.acquire()
            select_job_node = self.select_job_node
            self.select_job_node = None
            if select_job_node:
                logging.debug('shutting down scheduler ...')
                coros = []
                for uid, _job in self._sched_jobs.iteritems():
                    reply = _JobReply(_job, self.ip_addr, status=DispyJob.Terminated)
                    cluster = self._clusters[_job.compute_id]
                    compute = cluster._compute
                    coros.append(Coro(self.send_job_result, _job.uid, compute.id,
                                      cluster.client_scheduler_ip_addr,
                                      cluster.client_job_result_port, reply))
                clusters = self._clusters.values()
                self._clusters = {}
                self._sched_jobs = {}
                self._terminate_scheduler = True
                self._sched_cv.notify()
                yield self._sched_cv.release()

                for cluster in clusters:
                    compute = cluster._compute
                    for node in compute.nodes.itervalues():
                        yield node.close(compute, coro=coro)
                for coro in coros:
                    coro.value()
        Coro(_shutdown, self).value()
        self.timer_coro.terminate()
        self.request_coro.terminate()
        self.job_result_coro.terminate()
        self.udp_coro.terminate()
        self.asyncoro.join()
        self.asyncoro.terminate()

    def stats(self):
        print
        heading = '%020s  |  %05s  |  %05s  |  %13s' % \
                  ('Node', 'CPUs', 'Jobs', 'Node Time Sec')
        print heading
        print '-' * len(heading)
        tot_cpu_time = 0
        for ip_addr in sorted(self._nodes, key=lambda addr: self._nodes[addr].cpu_time,
                              reverse=True):
            node = self._nodes[ip_addr]
            tot_cpu_time += node.cpu_time
            print '%020s  |  %05s  |  %05s  |  %13.3f' % \
                  (ip_addr, node.cpus, node.jobs, node.cpu_time)
        wall_time = time.time() - self.start_time
        print
        print 'Total job time: %.3f sec' % (tot_cpu_time)
        print

    def close(self, compute_id):
        cluster = self._clusters.get(compute_id, None)
        if compute is not None:
            for ip_addr, node in self._nodes.iteritems():
                node.close(cluster._compute)
            del self._clusters[compute_id]

    def cleanup_computation(self, cluster):
        # non-generator, called with _sched_cv held
        if not cluster.zombie:
            return
        if cluster._pending_jobs:
            logging.debug('pedning jobs for "%s" / %s: %s',
                          cluster._compute.name, cluster._compute.id, cluster._pending_jobs)
            if cluster._pending_jobs > 0:
                return

        compute = cluster._compute
        nodes = compute.nodes.values()
        for node in nodes:
            try:
                node.clusters.remove(compute.id)
            except:
                logging.debug('Cluster %s/%s is already removed from %s',
                              compute.name, compute.id, node.ip_addr)
                pass
        compute.nodes = {}
        for xf in compute.xfer_files:
            logging.debug('Removing file "%s"', xf.name)
            if os.path.isfile(xf.name):
                try:
                    os.remove(xf.name)
                except:
                    logging.warning('Could not remove file "%s"', xf.name)
                    
        dest_path = os.path.join(self.dest_path_prefix, str(compute.id))
        if os.path.isdir(dest_path) and len(os.listdir(dest_path)) == 0:
            try:
                os.rmdir(dest_path)
            except:
                logging.warning('Could not remove directory "%s"', dest_path)

        del self._clusters[compute.id]
        for node in nodes:
            try:
                Coro(node.close, compute)
            except:
                logging.warning('Closing node %s failed', node.ip_addr)

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('-d', action='store_true', dest='loglevel', default=False,
                        help='if True, debug messages are printed')
    parser.add_argument('-n', '--nodes', action='append', dest='nodes', default=[],
                        help='name or IP address used for all computations; repeat for multiple nodes')
    parser.add_argument('-i', '--ip_addr', dest='ip_addr', default=None,
                        help='IP address to use (may be needed in case of multiple interfaces)')
    parser.add_argument('-p', '--port', dest='port', type=int, default=51347,
                        help='port number to use')
    parser.add_argument('--node_port', dest='node_port', type=int, default=51348,
                        help='port number used by nodes')
    parser.add_argument('--node_secret', dest='node_secret', default='',
                        help='authentication secret for handshake with dispy clients')
    parser.add_argument('--node_keyfile', dest='node_keyfile', default=None,
                        help='file containing SSL key to be used with nodes')
    parser.add_argument('--node_certfile', dest='node_certfile', default=None,
                        help='file containing SSL certificate to be used with nodes')
    parser.add_argument('--cluster_secret', dest='cluster_secret', default='',
                        help='file containing SSL certificate to be used with dispy clients')
    parser.add_argument('--cluster_certfile', dest='cluster_certfile', default=None,
                        help='file containing SSL certificate to be used with dispy clients')
    parser.add_argument('--cluster_keyfile', dest='cluster_keyfile', default=None,
                        help='file containing SSL key to be used with dispy clients')
    parser.add_argument('--pulse_interval', dest='pulse_interval', type=float, default=None,
                        help='number of seconds between pulse messages to indicate whether node is alive')
    parser.add_argument('--ping_interval', dest='ping_interval', type=float, default=None,
                        help='number of seconds between ping messages to discover nodes')
    parser.add_argument('--zombie_interval', dest='zombie_interval', default=60, type=float,
                        help='interval in minutes to presume unresponsive scheduler is zombie')
    parser.add_argument('--dest_path_prefix', dest='dest_path_prefix',
                        default=os.path.join(os.sep, 'tmp', 'dispyscheduler'),
                        help='path prefix where files sent by dispy are stored')
    parser.add_argument('--max_file_size', dest='max_file_size', default=None, type=int,
                        help='maximum file size of any file transferred')

    config = vars(parser.parse_args(sys.argv[1:]))
    if config['loglevel']:
        config['loglevel'] = logging.DEBUG
    else:
        config['loglevel'] = logging.INFO

    if config['zombie_interval']:
        config['zombie_interval'] = float(config['zombie_interval'])
        if config['zombie_interval'] < 1:
            raise Exception('zombie_interval must be at least 1')

    scheduler = _Scheduler(**config)
    while True:
        try:
            scheduler.asyncoro.join()
        except KeyboardInterrupt:
            logging.info('Interrupted; terminating')
            scheduler.shutdown()
            break
        except:
            logging.warning(traceback.format_exc())
            logging.warning('Scheduler terminated (possibly due to an error); restarting')
            time.sleep(2)
    scheduler.stats()
    exit(0)
