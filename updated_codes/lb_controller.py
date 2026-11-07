import csv, os, time
from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib import hub
from os_ken.lib.packet import packet, ethernet, arp, ipv4, tcp, ether_types

VIP = '10.0.0.100'
VMAC = '00:00:00:00:01:00'
SERVERS = [
    {'name': 'srv1', 'ip': '10.0.0.1', 'mac': '00:00:00:00:00:01'},
    {'name': 'srv2', 'ip': '10.0.0.2', 'mac': '00:00:00:00:00:02'},
    {'name': 'srv3', 'ip': '10.0.0.3', 'mac': '00:00:00:00:00:03'},
]
ALGORITHMS = ('rr', 'lc', 'load')
DEFAULT_ALGORITHM = os.environ.get('LB_ALGO', 'rr')
ASSIGN_LOG = os.environ.get('LB_LOG', 'assignments.csv')    # relative to where you start the controller
IDLE_TIMEOUT = int(os.environ.get('LB_IDLE', '30'))         # seconds a flow survives without traffic
POLL_INTERVAL = float(os.environ.get('LB_POLL', '2'))       # seconds between port-stats requests (F5)


class LoadBalancer(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connections = {}     # (client_ip, client_port, vip_port) -> server
        self.mac_to_port = {}
        self.dps = {}             # connected switches, for the stats poller
        self.server_names = [s['name'] for s in SERVERS]
        self.active = dict.fromkeys(self.server_names, 0)     # F4: live flows per server (never reset)
        self.rate = dict.fromkeys(self.server_names, 0.0)     # F5: bytes/s per server port (never reset)
        self.last_stats = {}      # server name -> (timestamp, rx+tx bytes)
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
        self.logger.info('LB: algorithm=%s idle_timeout=%ss poll=%ss, logging assignments to %s',
                         self.algorithm, IDLE_TIMEOUT, POLL_INTERVAL, ASSIGN_LOG)

    # ---------- F6: algorithm interface ----------
    def set_algorithm(self, algo):
        if algo not in ALGORITHMS:
            return False
        self.algorithm = algo
        self.reset_stats()
        self.logger.info('LB: algorithm set to %s (stats reset)', algo)
        return True

    def _pick_min(self, metric):
        # scan starting at rr_index so ties rotate instead of always picking srv1
        n = len(SERVERS)
        order = [SERVERS[(self.rr_index + i) % n] for i in range(n)]
        srv = min(order, key=metric)
        self.rr_index = (SERVERS.index(srv) + 1) % n
        return srv

    def pick_server(self):
        if self.algorithm == 'rr':
            srv = SERVERS[self.rr_index % len(SERVERS)]
            self.rr_index += 1
            return srv
        if self.algorithm == 'lc':
            return self._pick_min(lambda s: self.active[s['name']])
        if self.algorithm == 'load':
            return self._pick_min(lambda s: self.rate[s['name']])
        raise NotImplementedError(self.algorithm)

    # ---------- F7: statistics ----------
    def reset_stats(self):
        self.total = dict.fromkeys(self.server_names, 0)   # connections assigned per server
        self.rr_index = 0

    def jain(self):
        xs = list(self.total.values())
        s = sum(xs)
        return (s * s) / (len(xs) * sum(x * x for x in xs)) if s else 1.0

    def rates_mbps(self):
        return {n: round(r * 8 / 1e6, 2) for n, r in self.rate.items()}

    def record_assignment(self, srv, ip, tp):
        self.total[srv['name']] += 1
        self.active[srv['name']] += 1
        self._csv.writerow([time.strftime('%H:%M:%S'), self.algorithm, ip.src, tp.src_port, srv['name']])
        self._log.flush()
        self.logger.info('LB: NEW %s:%s -> %s [%s] total=%s active=%s rate_mbps=%s jain=%.3f',
                         ip.src, tp.src_port, srv['name'], self.algorithm,
                         self.total, self.active, self.rates_mbps(), self.jain())

    # ---------- F5: port-statistics polling ----------
    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change_handler(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.dps[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER:
            self.dps.pop(dp.id, None)
            self.last_stats.clear()
            self.rate = dict.fromkeys(self.server_names, 0.0)

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
        for s in SERVERS:
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
        srv = self.connections.pop(key, None)
        if srv is None:
            self.logger.info('LB: FLOW REMOVED %s:%s (no connection entry)', key[0], key[1])
            return
        if self.active[srv['name']] > 0:
            self.active[srv['name']] -= 1
        self.logger.info('LB: FLOW REMOVED %s:%s from %s, active=%s',
                         key[0], key[1], srv['name'], self.active)
