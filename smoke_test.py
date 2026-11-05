import subprocess, collections, time
from topo import build, start_servers, VIP

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

ok_all = []
def check(name, ok, info=''):
    ok_all.append(ok)
    print(('PASS  ' if ok else 'FAIL  ') + name, info)

net, servers, clients = build()
net.start()
time.sleep(3)
start_servers(servers)
time.sleep(1)

check('switch connected to controller', 'is_connected' in sh('ovs-vsctl list controller')
      and 'true' in sh('ovs-vsctl --columns=is_connected list controller'))
drop = net.pingAll()
check('pingall (plain L2 forwarding)', drop == 0, f'{drop}% dropped')

for s in servers:
    out = clients[0].cmd(f'curl -s --max-time 3 http://{s.IP()}/').strip()
    check(f'direct HTTP to {s.name} (bypasses VIP)',
          out == f'Hello from {s.name}',
          out or 'no reply')

counts = collections.Counter()
for _ in range(12):
    counts[clients[0].cmd(f'curl -s --max-time 2 http://{VIP}/').strip()] += 1
check('Round Robin: 12 requests -> 4/4/4', sorted(counts.values()) == [4, 4, 4], dict(counts))

check('client learned VIP MAC via ARP', '00:00:00:00:01:00' in clients[0].cmd('arp -n'))

flows = sh('ovs-ofctl -O OpenFlow13 dump-flows s1')
check('rewrite rules installed in switch',
      'set_field:10.0.0.100->ip_src' in flows and '->ip_dst' in flows)

got = [bool(c.cmd(f'curl -s --max-time 2 http://{VIP}/').strip()) for c in clients]
check('all 4 clients get a reply', all(got))

if not all(ok_all):
    print(sh('ovs-ofctl -O OpenFlow13 dump-flows s1'))

net.stop()
print('\nOVERALL:', 'ALL PASS' if all(ok_all) else 'SOME FAILED, paste this output')
