#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2011 thomasv@gitorious
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.


import random, socket, ast, re, ssl, errno
import threading, traceback, sys, time, json, Queue

from version import ELECTRUM_VERSION, PROTOCOL_VERSION
from util import print_error, print_msg


DEFAULT_TIMEOUT = 5
proxy_modes = ['socks4', 'socks5', 'http']


def pick_random_server():
    return random.choice( filter_protocol(DEFAULT_SERVERS,'s') )


class Interface(threading.Thread):


    def init_server(self, host, port, proxy=None, use_ssl=True):
        self.host = host
        self.port = port
        self.proxy = proxy
        self.use_ssl = use_ssl
        self.poll_interval = 1

        #json
        self.message_id = 0
        self.unanswered_requests = {}
        self.pending_transactions_for_notifications= []


    def queue_json_response(self, c):

        # uncomment to debug
        # print_error( "<--",c )

        msg_id = c.get('id')
        error = c.get('error')
        
        if error:
            print_error("received error:", c)
            if msg_id is not None:
                with self.lock: 
                    method, params, callback = self.unanswered_requests.pop(msg_id)
                callback(self,{'method':method, 'params':params, 'error':error, 'id':msg_id})

            return

        if msg_id is not None:
            with self.lock: 
                method, params, callback = self.unanswered_requests.pop(msg_id)
            result = c.get('result')

        else:
            # notification
            method = c.get('method')
            params = c.get('params')

            if method == 'blockchain.numblocks.subscribe':
                result = params[0]
                params = []

            elif method == 'blockchain.headers.subscribe':
                result = params[0]
                params = []

            elif method == 'blockchain.address.subscribe':
                addr = params[0]
                result = params[1]
                params = [addr]

            with self.lock:
                for k,v in self.subscriptions.items():
                    if (method, params) in v:
                        callback = k
                        break
                else:
                    print_error( "received unexpected notification", method, params)
                    print_error( self.subscriptions )
                    return


        callback(self, {'method':method, 'params':params, 'result':result, 'id':msg_id})


    def on_version(self, i, result):
        self.server_version = result


    def init_http(self, host, port, proxy=None, use_ssl=True):
        self.init_server(host, port, proxy, use_ssl)
        self.session_id = None
        self.is_connected = True
        self.connection_msg = ('https' if self.use_ssl else 'http') + '://%s:%d'%( self.host, self.port )
        try:
            self.poll()
        except:
            print_error("http init session failed")
            self.is_connected = False
            return

        if self.session_id:
            print_error('http session:',self.session_id)
            self.is_connected = True
        else:
            self.is_connected = False

    def run_http(self):
        self.is_connected = True
        while self.is_connected:
            try:
                if self.session_id:
                    self.poll()
                time.sleep(self.poll_interval)
            except socket.gaierror:
                break
            except socket.error:
                break
            except:
                traceback.print_exc(file=sys.stdout)
                break
            
        self.is_connected = False

                
    def poll(self):
        self.send([])


    def send_http(self, messages, callback):
        import urllib2, json, time, cookielib
        print_error( "send_http", messages )
        
        if self.proxy:
            import socks
            socks.setdefaultproxy(proxy_modes.index(self.proxy["mode"]) + 1, self.proxy["host"], int(self.proxy["port"]) )
            socks.wrapmodule(urllib2)

        cj = cookielib.CookieJar()
        opener = urllib2.build_opener(urllib2.HTTPCookieProcessor(cj))
        urllib2.install_opener(opener)

        t1 = time.time()

        data = []
        for m in messages:
            method, params = m
            if type(params) != type([]): params = [params]
            data.append( { 'method':method, 'id':self.message_id, 'params':params } )
            self.unanswered_requests[self.message_id] = method, params, callback
            self.message_id += 1

        if data:
            data_json = json.dumps(data)
        else:
            # poll with GET
            data_json = None 

            
        headers = {'content-type': 'application/json'}
        if self.session_id:
            headers['cookie'] = 'SESSION=%s'%self.session_id

        try:
            req = urllib2.Request(self.connection_msg, data_json, headers)
            response_stream = urllib2.urlopen(req, timeout=DEFAULT_TIMEOUT)
        except:
            return

        for index, cookie in enumerate(cj):
            if cookie.name=='SESSION':
                self.session_id = cookie.value

        response = response_stream.read()
        self.bytes_received += len(response)
        if response: 
            response = json.loads( response )
            if type(response) is not type([]):
                self.queue_json_response(response)
            else:
                for item in response:
                    self.queue_json_response(item)

        if response: 
            self.poll_interval = 1
        else:
            if self.poll_interval < 15: 
                self.poll_interval += 1
        #print self.poll_interval, response

        self.rtime = time.time() - t1
        self.is_connected = True




    def init_tcp(self, host, port, proxy=None, use_ssl=True):
        self.init_server(host, port, proxy, use_ssl)

        global proxy_modes
        self.connection_msg = "%s:%d"%(self.host,self.port)
        if self.proxy is None:
            s = socket.socket( socket.AF_INET, socket.SOCK_STREAM )
        else:
            self.connection_msg += " using proxy %s:%s:%s"%(self.proxy.get('mode'), self.proxy.get('host'), self.proxy.get('port'))
            import socks
            s = socks.socksocket()
            s.setproxy(proxy_modes.index(self.proxy["mode"]) + 1, self.proxy["host"], int(self.proxy["port"]) )

        if self.use_ssl:
            s = ssl.wrap_socket(s, ssl_version=ssl.PROTOCOL_SSLv23, do_handshake_on_connect=True)
            
        s.settimeout(2)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)

        try:
            s.connect(( self.host.encode('ascii'), int(self.port)))
        except:
            #traceback.print_exc(file=sys.stdout)
            print_error("failed to connect", host, port)
            self.is_connected = False
            self.s = None
            return

        s.settimeout(60)
        self.s = s
        self.is_connected = True

    def run_tcp(self):
        try:
            #if self.use_ssl: self.s.do_handshake()
            out = ''
            while self.is_connected:
                try: 
                    timeout = False
                    msg = self.s.recv(1024)
                except socket.timeout:
                    timeout = True
                except ssl.SSLError:
                    timeout = True
                except socket.error, err:
                    if err.errno in [11, 10035]:
                        print_error("socket errno", err.errno)
                        time.sleep(0.1)
                        continue
                    else:
                        traceback.print_exc(file=sys.stdout)
                        raise

                if timeout:
                    # ping the server with server.version, as a real ping does not exist yet
                    self.send([('server.version', [ELECTRUM_VERSION, PROTOCOL_VERSION])], self.on_version)
                    continue

                out += msg
                self.bytes_received += len(msg)
                if msg == '': 
                    self.is_connected = False

                while True:
                    s = out.find('\n')
                    if s==-1: break
                    c = out[0:s]
                    out = out[s+1:]
                    c = json.loads(c)
                    self.queue_json_response(c)

        except:
            traceback.print_exc(file=sys.stdout)

        self.is_connected = False


    def send_tcp(self, messages, callback):
        """return the ids of the requests that we sent"""
        out = ''
        ids = []
        for m in messages:
            method, params = m 
            request = json.dumps( { 'id':self.message_id, 'method':method, 'params':params } )
            self.unanswered_requests[self.message_id] = method, params, callback
            ids.append(self.message_id)
            # uncomment to debug
            # print "-->", request
            self.message_id += 1
            out += request + '\n'
        while out:
            try:
                sent = self.s.send( out )
                out = out[sent:]
            except socket.error,e:
                if e[0] in (errno.EWOULDBLOCK,errno.EAGAIN):
                    print_error( "EAGAIN: retrying")
                    time.sleep(0.1)
                    continue
                else:
                    traceback.print_exc(file=sys.stdout)
                    # this happens when we get disconnected
                    print_error( "Not connected, cannot send" )
                    return None
        return ids



    def __init__(self, config=None):
        #self.server = random.choice(filter_protocol(DEFAULT_SERVERS, 's'))
        self.proxy = None

        if config is None:
            from simple_config import SimpleConfig
            config = SimpleConfig()

        threading.Thread.__init__(self)
        self.daemon = True
        self.config = config
        self.connect_event = threading.Event()

        self.subscriptions = {}
        self.lock = threading.Lock()

        self.servers = {} # actual list from IRC
        self.rtime = 0
        self.bytes_received = 0
        self.is_connected = False

        # init with None server, in case we are offline 
        self.init_server(None, None)




    def init_interface(self):
        if self.config.get('server'):
            self.init_with_server(self.config)
        else:
            if self.config.get('auto_cycle') is None:
                self.config.set_key('auto_cycle', True, False)

        if not self.is_connected: 
            self.connect_event.set()
            return

        self.connect_event.set()


    def init_with_server(self, config):
            
        s = config.get('server')
        host, port, protocol = s.split(':')
        port = int(port)

        self.protocol = protocol
        proxy = self.parse_proxy_options(config.get('proxy'))
        self.server = host + ':%d:%s'%(port, protocol)

        #print protocol, host, port
        if protocol in 'st':
            self.init_tcp(host, port, proxy, use_ssl=(protocol=='s'))
        elif protocol in 'gh':
            self.init_http(host, port, proxy, use_ssl=(protocol=='g'))
        else:
            raise BaseException('Unknown protocol: %s'%protocol)


    def stop_subscriptions(self):
        for callback in self.subscriptions.keys():
            callback(self, None)
        self.subscriptions = {}


    def send(self, messages, callback):

        sub = []
        for message in messages:
            m, v = message
            if m[-10:] == '.subscribe':
                sub.append(message)

        if sub:
            with self.lock:
                if self.subscriptions.get(callback) is None: 
                    self.subscriptions[callback] = []
                for message in sub:
                    if message not in self.subscriptions[callback]:
                        self.subscriptions[callback].append(message)

        if not self.is_connected: 
            return

        if self.protocol in 'st':
            with self.lock:
                out = self.send_tcp(messages, callback)
        else:
            # do not use lock, http is synchronous
            out = self.send_http(messages, callback)

        return out


    def parse_proxy_options(self, s):
        if type(s) == type({}): return s  # fixme: type should be fixed
        if type(s) != type(""): return None  
        if s.lower() == 'none': return None
        proxy = { "mode":"socks5", "host":"localhost" }
        args = s.split(':')
        n = 0
        if proxy_modes.count(args[n]) == 1:
            proxy["mode"] = args[n]
            n += 1
        if len(args) > n:
            proxy["host"] = args[n]
            n += 1
        if len(args) > n:
            proxy["port"] = args[n]
        else:
            proxy["port"] = "8080" if proxy["mode"] == "http" else "1080"
        return proxy



    def stop(self):
        if self.is_connected and self.protocol in 'st' and self.s:
            self.s.shutdown(socket.SHUT_RDWR)
            self.s.close()


    def is_up_to_date(self):
        return self.unanswered_requests == {}


    def synchronous_get(self, requests, timeout=100000000):
        # todo: use generators, unanswered_requests should be a list of arrays...
        queue = Queue.Queue()
        ids = self.send(requests, lambda i,r: queue.put(r))
        id2 = ids[:]
        res = {}
        while ids:
            r = queue.get(True, timeout)
            _id = r.get('id')
            if _id in ids:
                ids.remove(_id)
                res[_id] = r.get('result')
        out = []
        for _id in id2:
            out.append(res[_id])
        return out


    def start(self, queue):
        self.queue = queue
        threading.Thread.start(self)


    def run(self):
        self.init_interface()
        if self.is_connected:
            self.send([('server.version', [ELECTRUM_VERSION, PROTOCOL_VERSION])], self.on_version)
            self.change_status()
            self.run_tcp() if self.protocol in 'st' else self.run_http()
        self.change_status()
        

    def change_status(self):
        #print "change status", self.server, self.is_connected
        self.queue.put(self)

