import subprocess, collections, time, csv
from topo import build, start_servers, VIP

def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout

def csv_rows():
    try:
        with open('assignments.csv') as f:
            return list(csv.reader(f))[1:]
    except OSError:
        return []

ok_all = []
def check(name, ok, info=''):
    ok_all.append(ok)
    print(('PASS  ' if ok else 'FAIL  ') + name, info)

def wait_for_controller(timeout=10):
    for _ in range(timeout * 2):
        if 'CONTROLLER' in sh('ovs-ofctl -O OpenFlow13 dump-flows s1'):
            return True
        time.sleep(0.5)
    return False

net, servers, clients = build()
net.start()
time.sleep(3)
start_servers(servers)
time.sleep(1)
check('switch connected to controller (table-miss rule installed)', wait_for_controller())
net.pingAll()                                   # controller learns every host's port
time.sleep(5)                                   # let the poller store baselines

# 1) background load: one iperf flow to the VIP (the controller picks the server)
before = len(csv_rows())
iperf = clients[0].popen(['iperf', '-c', VIP, '-t', '25'],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(2)
rows = csv_rows()[before:]
check('iperf connection assigned', len(rows) == 1 and rows[0][1] == 'load',
      [r[4] for r in rows])
loaded = rows[0][4] if rows else None

# 2) wait for >= 2 poll intervals so the controller has measured the rate
time.sleep(7)

# 3) six short requests: none may go to the loaded server
before = len(csv_rows())
for _ in range(6):
    clients[1].cmd(f'curl -s --max-time 3 http://{VIP}/')
    time.sleep(0.3)
picked = [r[4] for r in csv_rows()[before:]]
check(f'Load-aware: 6 requests avoid the loaded server ({loaded})',
      len(picked) == 6 and loaded not in picked, picked)
check('requests spread over the two idle servers', len(set(picked)) == 2, dict(collections.Counter(picked)))

if iperf.poll() is None:
    iperf.terminate()
net.stop()
print('\nOVERALL:', 'ALL PASS' if all(ok_all) else 'SOME FAILED, paste this output')
