from os_ken.base import app_manager
from os_ken.controller import ofp_event
from os_ken.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, set_ev_cls
from os_ken.ofproto import ofproto_v1_3
from os_ken.lib.packet import packet, ethernet, arp, ipv4, tcp, ether_types

VIP = '10.0.0.100'
VMAC = '00:00:00:00:01:00'
SERVERS = [
    {'name': 'srv1', 'ip': '10.0.0.1', 'mac': '00:00:00:00:00:01'},
    {'name': 'srv2', 'ip': '10.0.0.2', 'mac': '00:00:00:00:00:02'},
    {'name': 'srv3', 'ip': '10.0.0.3', 'mac': '00:00:00:00:00:03'},
]
ALGORITHM = 'rr'


class LoadBalancer(app_manager.OSKenApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.connections = {}
        self.mac_to_port = {}
        self.rr_index = 0

    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        dp = ev.msg.datapath
        parser, ofp = dp.ofproto_parser, dp.ofproto
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)]
        self.add_flow(dp, 0, match, actions)
        self.logger.info('Switch %s connected', dp.id)

    def add_flow(self, dp, priority, match, actions, idle=0):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)]
        dp.send_msg(parser.OFPFlowMod(datapath=dp, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle))

    def send_packet(self, dp, in_port, actions, data):
        parser, ofp = dp.ofproto_parser, dp.ofproto
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=in_port, actions=actions, data=data))

    def pick_server(self):
        if ALGORITHM == 'rr':
            srv = SERVERS[self.rr_index % len(SERVERS)]
            self.rr_index += 1
            return srv
        raise NotImplementedError('other algorithms: Adeena (F4-F6)')

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
            self.connections[key] = srv
            self.logger.info('LB: NEW %s:%s -> %s', ip.src, tp.src_port, srv['name'])
        else:
            self.logger.info('LB: KNOWN %s:%s stays on %s (flags=0x%02x)', ip.src, tp.src_port, srv['name'], tp.bits)

        srv_port = self.mac_to_port.get(srv['mac'], ofp.OFPP_FLOOD)

        fwd_match = parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=ip.src, ipv4_dst=VIP, tcp_src=tp.src_port, tcp_dst=tp.dst_port)
        fwd_actions = [parser.OFPActionSetField(eth_dst=srv['mac']),
                       parser.OFPActionSetField(ipv4_dst=srv['ip']),
                       parser.OFPActionOutput(srv_port)]
        self.add_flow(dp, 10, fwd_match, fwd_actions, idle=30)

        rev_match = parser.OFPMatch(eth_type=0x0800, ip_proto=6, ipv4_src=srv['ip'],
                                    ipv4_dst=ip.src, tcp_src=tp.dst_port, tcp_dst=tp.src_port)
        rev_actions = [parser.OFPActionSetField(eth_src=VMAC),
                       parser.OFPActionSetField(ipv4_src=VIP),
                       parser.OFPActionOutput(in_port)]
        self.add_flow(dp, 10, rev_match, rev_actions, idle=30)

        self.send_packet(dp, in_port, fwd_actions, msg.data)
