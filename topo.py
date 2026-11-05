#!/usr/bin/env python3
# topo.py  -  Mininet topology: 4 clients, 3 web servers, 1 OpenFlow switch
# Run with:  sudo python3 topo.py         (interactive)
#            sudo python3 topo.py --auto  (runs a request-distribution test)
import sys, time, collections
from mininet.net import Mininet
from mininet.node import RemoteController, OVSSwitch
from mininet.link import TCLink
from mininet.cli import CLI
from mininet.log import setLogLevel
 
VIP = '10.0.0.100'
N_SERVERS, N_CLIENTS = 3, 4
 
 
def build():
    net = Mininet(controller=None, switch=OVSSwitch, link=TCLink, autoSetMacs=False)
    net.addController('c0', controller=RemoteController, ip='127.0.0.1', port=6653)
    s1 = net.addSwitch('s1', protocols='OpenFlow13')
    servers, clients = [], []
    for i in range(1, N_SERVERS + 1):
        h = net.addHost(f'srv{i}', ip=f'10.0.0.{i}/24', mac=f'00:00:00:00:00:0{i}')
        net.addLink(h, s1, bw=10)          # 10 Mbit/s link (change later to test)
        servers.append(h)
    for i in range(1, N_CLIENTS + 1):
        h = net.addHost(f'c{i}', ip=f'10.0.0.{10 + i}/24', mac=f'00:00:00:00:00:{10 + i}')
        net.addLink(h, s1, bw=10)
        clients.append(h)
    return net, servers, clients
 
 
def start_servers(servers):
    for s in servers:
        s.cmd(f'mkdir -p /tmp/{s.name} && echo "Hello from {s.name}" > /tmp/{s.name}/index.html')
        s.cmd(f'cd /tmp/{s.name} && python3 -m http.server 80 > /dev/null 2>&1 &')
        s.cmd('iperf -s > /dev/null 2>&1 &')
 
 
def distribution_test(client, requests=30):
    counts = collections.Counter()
    for _ in range(requests):
        counts[client.cmd(f'curl -s --max-time 2 http://{VIP}/').strip()] += 1
    print(f'\n{requests} requests from {client.name} to {VIP}:')
    for reply, n in sorted(counts.items()):
        print(f'  {reply:20s} {n}')
 
 
if __name__ == '__main__':
    setLogLevel('info')
    net, servers, clients = build()
    net.start()
    time.sleep(3)                 # let the switch connect to the controller
    start_servers(servers)
    net.pingAll()                 # lets the controller learn every host's port
    if '--auto' in sys.argv:
        distribution_test(clients[0])
    else:
        CLI(net)
    net.stop()
