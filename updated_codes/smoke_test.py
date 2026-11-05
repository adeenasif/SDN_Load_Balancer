import subprocess, collections, time, csv
from topo import build, start_servers, VIP

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

def csv_rows():
    try:
        with open('assignments.csv') as f:      # written by the controller; run from the same folder
            return list(csv.reader(f))[1:]
    except OSError:
        return []

ok_all = []
def check(name, ok, info=''):
    ok_all.append(ok)
    print(('PASS  ' if ok else 'FAIL  ') + name, info)

net, servers, clients = build()
net.start()
time.sleep(3)
start_servers(servers)
time.sleep(1)

def wait_for_controller(timeout=10):
    for _ in range(timeout * 2):
        if 'CONTROLLER' in sh('ovs-ofctl -O OpenFlow13 dump-flows s1'):
            return True
        time.sleep(0.5)
    return False

check('switch connected to controller (table-miss rule installed)', wait_for_controller())
#check('switch connected to controller', 'is_connected' in sh('ovs-vsctl list Controller')
#      and 'true' in sh('ovs-vsctl --columns=is_connected list Controller'))
drop = net.pingAll()
check('pingall (plain L2 forwarding)', drop == 0, f'{drop}% dropped')

check('srv3 link is 2 Mbit', '2Mbit' in servers[2].cmd('tc class show dev srv3-eth0'))
check('srv1 link is 10 Mbit', '10Mbit' in servers[0].cmd('tc class show dev srv1-eth0'))

for s in servers:
    out = clients[0].cmd(f'curl -s --max-time 3 http://{s.IP()}/').strip()
    check(f'direct HTTP to {s.name} (bypasses VIP)', out == f'Hello from {s.name}', out or 'no reply')

before = len(csv_rows())
counts = collections.Counter()
for _ in range(12):
    counts[clients[0].cmd(f'curl -s --max-time 2 http://{VIP}/').strip()] += 1
check('Round Robin: 12 requests -> 4/4/4', sorted(counts.values()) == [4, 4, 4], dict(counts))

new = csv_rows()[before:]
logged = collections.Counter(r[4] for r in new)
check('F7: controller logged 12 assignments, 4/4/4, algorithm=rr',
      len(new) == 12 and sorted(logged.values()) == [4, 4, 4] and all(r[1] == 'rr' for r in new),
      dict(logged))

check('client learned VIP MAC via ARP', '00:00:00:00:01:00' in clients[0].cmd('arp -n'))

flows = sh('ovs-ofctl -O OpenFlow13 dump-flows s1')
check('rewrite rules installed in switch',
      'set_field:10.0.0.100->ip_src' in flows and '->ip_dst' in flows)

got = [bool(c.cmd(f'curl -s --max-time 2 http://{VIP}/').strip()) for c in clients]
check('all 4 clients get a reply', all(got), got)

if not all(ok_all):
    print(flows)

net.stop()
print('\nOVERALL:', 'ALL PASS' if all(ok_all) else 'SOME FAILED, paste this output')