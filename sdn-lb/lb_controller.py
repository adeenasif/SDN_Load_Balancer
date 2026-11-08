import csv, os, time, collections, json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib import hub
from os_ken.lib.packet import packet, ethernet, arp, ipv4, tcp, ether_types

VIP = '10.0.0.100'
VMAC = '00:00:00:00:01:00'
# Default server pool (matches topo.py). `cap` is the link capacity in Mbit/s (srv3 is the slow 2 Mbit/s one).
# The GUI replaces this list through POST /lb/topology when you change the number of servers.
DEFAULT_SERVERS = [
    {'name': 'srv1', 'ip': '10.0.0.1', 'mac': '00:00:00:00:00:01', 'cap': 10.0},
    {'name': 'srv2', 'ip': '10.0.0.2', 'mac': '00:00:00:00:00:02', 'cap': 10.0},
    {'name': 'srv3', 'ip': '10.0.0.3', 'mac': '00:00:00:00:00:03', 'cap': 2.0},
]
# Load-aware: until the next port-stats reply shows its real traffic, a brand-new connection is assumed to carry
# this many Mbit (divided by the poll interval to get Mbit/s) on its server. It stops several simultaneous SYNs from all choosing the same "idle" server.
EST_CONN_MBIT = float(os.environ.get('LB_EST_MBIT', os.environ.get('LB_EST_MBPS', '0.3')))   # Mbit per new connection (40 KB page = 0.32)
# Load-aware scoring: 'norm' (default) = traffic rate / link capacity (+ pending estimate);
# 'raw' = the proposal's literal F5, the plain traffic rate in Mbit/s, for comparison.
LOAD_MODE = os.environ.get('LB_LOAD_MODE', 'norm')
ALGORITHMS = ('rr', 'lc', 'load')
DEFAULT_ALGORITHM = os.environ.get('LB_ALGO', 'rr')
ASSIGN_LOG = os.environ.get('LB_LOG', 'assignments.csv')    # relative to where you start the controller
IDLE_TIMEOUT = int(os.environ.get('LB_IDLE', '30'))         # seconds a flow survives without traffic
POLL_INTERVAL = float(os.environ.get('LB_POLL', '2'))       # seconds between port-stats requests (F5)
REST_PORT = int(os.environ.get('LB_PORT', '8080'))


class LoadBalancer(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()                       # REST thread <-> OpenFlow handlers
        self.connections = {}     # (client_ip, client_port, vip_port) -> server
        self.mac_to_port = {}
        self.dps = {}             # connected switches, for the stats poller
        self.servers = [dict(x) for x in DEFAULT_SERVERS]
        self.server_names = [s['name'] for s in self.servers]
        self.active = dict.fromkeys(self.server_names, 0)     # F4: live flows per server (never reset)
        self.rate = dict.fromkeys(self.server_names, 0.0)     # F5: bytes/s per server port (never reset)
        self.pending = dict.fromkeys(self.server_names, 0)    # F5: assignments since the last stats reply
        self.last_stats = {}      # server name -> (timestamp, rx+tx bytes)
        self.recent_events = collections.deque(maxlen=200)    # recent assignments for the GUI
        self.seq = 0              # grows forever, so the GUI can tell which events are new
        self.reset_stats()
        self.algorithm = 'rr'
        if DEFAULT_ALGORITHM in ALGORITHMS:
            self.algorithm = DEFAULT_ALGORITHM
        else:
            self.logger.warning('LB: unknown LB_ALGO=%s, using rr', DEFAULT_ALGORITHM)
        self._log = open(ASSIGN_LOG, 'w', newline='')
        self._csv = csv.writer(self._log)
        self._csv.writerow(['time', 'algorithm', 'client_ip', 'client_port', 'server'])
        self._log.flush()
        self.monitor_thread = hub.spawn(self._monitor)
        # The REST API uses the standard library in a normal thread, so it works with every
        # os-ken version (hub.WSGIServer only exists when the eventlet hub is selected).
        threading.Thread(target=self._serve_rest, daemon=True).start()
        self.logger.info('LB: algorithm=%s idle_timeout=%ss poll=%ss, logging assignments to %s',
                         self.algorithm, IDLE_TIMEOUT, POLL_INTERVAL, ASSIGN_LOG)

    # ---------- F6: algorithm interface ----------
    def set_algorithm(self, algo):
        if algo not in ALGORITHMS:
            return False
        with self.lock:
            self.algorithm = algo
            self.reset_stats()
        self.logger.info('LB: algorithm set to %s (stats reset)', algo)
        return True

    def _pick_min(self, metric):
        # scan starting at rr_index so ties rotate instead of always picking srv1
        n = len(self.servers)
        order = [self.servers[(self.rr_index + i) % n] for i in range(n)]
        srv = min(order, key=metric)
        self.rr_index = (self.servers.index(srv) + 1) % n
        return srv

    def pick_server(self):
        if self.algorithm == 'rr':
            srv = self.servers[self.rr_index % len(self.servers)]
            self.rr_index += 1
            return srv
        if self.algorithm == 'lc':
            return self._pick_min(lambda s: self.active[s['name']])
        if self.algorithm == 'load':
            # F5 (load_norm): utilisation = measured traffic / link capacity, so the 2 Mbit/s srv3 counts as
            # "busy" much sooner than a 10 Mbit/s server. Ties go to the server with fewer live connections.
            return self._pick_min(lambda s: (self._load_score(s), self.active[s['name']]))
        raise NotImplementedError(self.algorithm)

    def _load_score(self, s):
        n = s['name']
        if LOAD_MODE == 'raw':
            return self.rate[n] * 8 / 1e6
        # a connection's bits show up in the next stats reply spread over one poll interval
        mbps = self.rate[n] * 8 / 1e6 + self.pending[n] * EST_CONN_MBIT / POLL_INTERVAL
        return mbps / s['cap']

    def configure_servers(self, servers):
        """Replace the server pool (the GUI does this when the Mininet topology is rebuilt)."""
        with self.lock:
            self.servers = [{'name': s['name'], 'ip': s['ip'], 'mac': s['mac'], 'cap': float(s['cap'])} for s in servers]
            self.server_names = [s['name'] for s in self.servers]
            self._clear_network_state()
            self.reset_stats()
            self.recent_events.clear()
        self.logger.info('LB: server pool set to %s', [(s['name'], s['cap']) for s in self.servers])

    def _clear_network_state(self):
        self.connections.clear()
        self.mac_to_port.clear()
        self.last_stats.clear()
        self.active = dict.fromkeys(self.server_names, 0)
        self.rate = dict.fromkeys(self.server_names, 0.0)
        self.pending = dict.fromkeys(self.server_names, 0)

    # ---------- F7: statistics ----------
    def reset_stats(self):
        self.total = dict.fromkeys(self.server_names, 0)   # connections assigned per server
        self.rr_index = 0

    def jain(self):
        xs = list(self.total.values())
        s = sum(xs)
        return (s * s) / (len(xs) * sum(x * x for x in xs)) if s else 1.0

    def jain_cap(self):
        """Jain's index of load per unit of capacity (connections / link Mbit/s): 1.0 = every server equally loaded
        relative to its link, which is the fair outcome when the links have unequal capacity."""
        xs = [self.total[s['name']] / s['cap'] for s in self.servers]
        s = sum(xs)
        return (s * s) / (len(xs) * sum(x * x for x in xs)) if s else 1.0

    # ---------- REST API (used by the GUI server, gui_server.py) ----------
    def snapshot(self):
        with self.lock:
            return {'algorithm': self.algorithm, 'algorithms': list(ALGORITHMS),
                    'jain': round(self.jain(), 3), 'jain_cap': round(self.jain_cap(), 3), 'poll_s': POLL_INTERVAL, 'idle_s': IDLE_TIMEOUT,
                    'switches': len(self.dps), 'seq': self.seq,
                    'ready': all(s['mac'] in self.mac_to_port for s in self.servers),
                    'servers': [{'name': n, 'active': self.active[n], 'total': self.total[n],
                                 'rate_mbps': round(self.rate[n] * 8 / 1e6, 3),
                                 'capacity_mbps': next(x['cap'] for x in self.servers if x['name'] == n)}
                                for n in self.server_names],
                    'events': list(self.recent_events)[:80]}

    def _serve_rest(self):
        ctl = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # keep the controller terminal quiet
                pass

            def _reply(self, code, body, ctype='application/json'):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                try:
                    if self.path == '/lb/stats':
                        return self._reply(200, ctl.snapshot())
                    if self.path == '/lb/assignments.csv':
                        with open(ASSIGN_LOG, 'rb') as f:
                            return self._reply(200, f.read(), 'text/csv; charset=utf-8')
                    if self.path == '/':
                        return self._reply(200, b'SDN load balancer controller API. '
                                                b'Open the GUI served by gui_server.py (port 5000).\n',
                                           'text/plain; charset=utf-8')
                except Exception as e:
                    ctl.logger.exception('REST error')
                    return self._reply(500, {'error': str(e)})
                self._reply(404, {'error': 'not found'})

            def do_POST(self):
                try:
                    n = int(self.headers.get('Content-Length') or 0)
                    body = json.loads(self.rfile.read(n)) if n else {}
                    if self.path == '/lb/reset':
                        with ctl.lock:
                            ctl.reset_stats()
                            ctl.recent_events.clear()
                        return self._reply(200, {'ok': True})
                    if self.path == '/lb/topology':
                        pool = body.get('servers')
                        if (not isinstance(pool, list) or not 1 <= len(pool) <= 8 or
                                any(not all(k in x for k in ('name', 'ip', 'mac', 'cap')) for x in pool)):
                            return self._reply(400, {'ok': False, 'error': 'servers: list of {name, ip, mac, cap}'})
                        ctl.configure_servers(pool)
                        return self._reply(200, {'ok': True, 'servers': len(pool)})
                    if self.path == '/lb/algorithm':
                        if ctl.set_algorithm(body.get('algorithm')):
                            return self._reply(200, {'ok': True, 'algorithm': ctl.algorithm})
                        return self._reply(400, {'ok': False, 'error': 'unknown algorithm'})
                except Exception as e:
                    ctl.logger.exception('REST error')
                    return self._reply(500, {'error': str(e)})
                self._reply(404, {'error': 'not found'})

        try:
            ThreadingHTTPServer(('0.0.0.0', REST_PORT), Handler).serve_forever()
        except OSError as e:
            self.logger.error('LB: cannot start REST API on port %s: %s', REST_PORT, e)

    def rates_mbps(self):
        return {n: round(r * 8 / 1e6, 2) for n, r in self.rate.items()}

    def record_assignment(self, srv, ip, tp):
        with self.lock:
            self.seq += 1
            self.recent_events.appendleft({'seq': self.seq, 'ts': time.time(), 't': time.strftime('%H:%M:%S'),
                                           'client': f'{ip.src}:{tp.src_port}', 'client_ip': ip.src,
                                           'srv': srv['name'], 'algo': self.algorithm})
            self.total[srv['name']] += 1
            self.active[srv['name']] += 1
            self.pending[srv['name']] += 1
            self._csv.writerow([time.strftime('%H:%M:%S'), self.algorithm, ip.src, tp.src_port, srv['name']])
            self._log.flush()
            self.logger.info('LB: NEW %s:%s -> %s [%s] total=%s active=%s rate_mbps=%s jain=%.3f',
                             ip.src, tp.src_port, srv['name'], self.algorithm,
                             self.total, self.active, self.rates_mbps(), self.jain())

    # ---------- F5: port-statistics polling ----------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        with self.lock:
            if ev.state == MAIN_DISPATCHER:
                self.dps[dp.id] = dp
            elif ev.state == DEAD_DISPATCHER:
                self.dps.pop(dp.id, None)
                self._clear_network_state()        # switch gone: its flows and learned ports are gone too

    def _monitor(self):
        while True:
            for dp in list(self.dps.values()):
                parser, ofp = dp.ofproto_parser, dp.ofproto
                dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
            hub.sleep(POLL_INTERVAL)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def port_stats_reply_handler(self, ev):
        now = time.monotonic()
        by_port = {st.port_no: st for st in ev.msg.body}
        with self.lock:
            for s in self.servers:
                st = by_port.get(self.mac_to_port.get(s['mac']))
                if st is None:
                    continue                              # port not learned yet
                total = st.rx_bytes + st.tx_bytes
                prev = self.last_stats.get(s['name'])
                if prev is not None:                      # first reading only stores a baseline
                    dt = now - prev[0]
                    if dt > 0:
                        self.rate[s['name']] = (total - prev[1]) / dt if total >= prev[1] else 0.0
                self.last_stats[s['name']] = (now, total)
            self.pending = dict.fromkeys(self.server_names, 0)     # real traffic is now visible in self.rate

    # ---------- switch plumbing ----------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        parser, ofp = dp.ofproto_parser, dp.ofproto
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions)
        self.logger.info('Switch %s connected', dp.id)

    def add_flow(self, dp, priority, match, actions, idle=0, flags=0):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle, flags=flags))

    def send_packet(self, dp, in_port, actions, data):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=data))

    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match['in_port']
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return
        with self.lock:
            self.mac_to_port[eth.src] = in_port

        arp_pkt = pkt.get_protocol(arp.arp)
        if arp_pkt and arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == VIP:
            self.answer_arp_for_vip(dp, in_port, arp_pkt)
            return

        ip = pkt.get_protocol(ipv4.ipv4)
        tp = pkt.get_protocol(tcp.tcp)
        if ip and tp and ip.dst == VIP:
            self.load_balance(dp, msg, in_port, ip, tp)
            return

        out_port = self.mac_to_port.get(eth.dst, dp.ofproto.OFPP_FLOOD)
        self.send_packet(dp, in_port, [dp.ofproto_parser.OFPActionOutput(out_port)], msg.data)

    def answer_arp_for_vip(self, dp, in_port, req):
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst=req.src_mac, src=VMAC))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=VMAC, src_ip=VIP,
                                 dst_mac=req.src_mac, dst_ip=req.src_ip))
        pkt.serialize()
        self.send_packet(dp, dp.ofproto.OFPP_CONTROLLER,
                         [dp.ofproto_parser.OFPActionOutput(in_port)], pkt.data)

    def load_balance(self, dp, msg, in_port, ip, tp):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        key = (ip.src, tp.src_port, tp.dst_port)
        with self.lock:
            srv = self.connections.get(key)
            if srv is None:
                srv = self.pick_server()
                if srv['mac'] not in self.mac_to_port:
                    self.logger.warning('LB: port of %s unknown, dropping this SYN (client retries)', srv['name'])
                    return
                self.connections[key] = srv
                self.record_assignment(srv, ip, tp)
            else:
                self.logger.info('LB: KNOWN %s:%s stays on %s (flags=0x%02x)',
                                 ip.src, tp.src_port, srv['name'], tp.bits)
            srv_port = self.mac_to_port[srv['mac']]

        fwd_match = parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=ip.src, ipv4_dst=VIP,
                                    tcp_src=tp.src_port, tcp_dst=tp.dst_port)
        fwd_actions = [parser.OFPActionSetField(eth_dst=srv['mac']),
                       parser.OFPActionSetField(ipv4_dst=srv['ip']),
                       parser.OFPActionOutput(srv_port)]
        # only the forward flow asks for a flow-removed message (one decrement per connection)
        self.add_flow(dp, 10, fwd_match, fwd_actions, idle=IDLE_TIMEOUT,
                      flags=ofp.OFPFF_SEND_FLOW_REM)

        rev_match = parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=srv['ip'],
                                    ipv4_dst=ip.src, tcp_src=tp.dst_port, tcp_dst=tp.src_port)
        rev_actions = [parser.OFPActionSetField(eth_src=VMAC),
                       parser.OFPActionSetField(ipv4_src=VIP),
                       parser.OFPActionOutput(in_port)]
        self.add_flow(dp, 10, rev_match, rev_actions, idle=IDLE_TIMEOUT)

        self.send_packet(dp, in_port, fwd_actions, msg.data)

    # ---------- F4: decrement when the forward flow expires ----------
    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def flow_removed_handler(self, ev):
        m = ev.msg.match
        try:
            key = (m['ipv4_src'], m['tcp_src'], m['tcp_dst'])
        except KeyError:
            return                                   # not one of our connection flows
        with self.lock:
            srv = self.connections.pop(key, None)
            if srv is None:
                self.logger.info('LB: FLOW REMOVED %s:%s (no connection entry)', key[0], key[1])
                return
            if self.active[srv['name']] > 0:
                self.active[srv['name']] -= 1
            self.logger.info('LB: FLOW REMOVED %s:%s from %s, active=%s',
                             key[0], key[1], srv['name'], self.active)