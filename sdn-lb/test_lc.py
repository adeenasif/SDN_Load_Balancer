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

# a 20 MB file per server so a rate-limited download stays open for a long time
for s in servers:
    s.cmd(f'dd if=/dev/zero of=/tmp/{s.name}/big.bin bs=1M count=20 2>/dev/null')
time.sleep(1)

# 1) three long-lived connections from c1
before = len(csv_rows())
longs = []
for _ in range(3):
    longs.append(clients[0].popen(['curl', '-s', '--limit-rate', '20k', '-o', '/dev/null',
                                   f'http://{VIP}/big.bin']))
    time.sleep(1.5)
rows = csv_rows()[before:]
picked = [r[4] for r in rows]
check('LC: 3 long connections -> 3 different servers', len(set(picked)) == 3 and len(rows) == 3, picked)
check('algorithm column is lc (start the controller with LB_ALGO=lc)',
      len(rows) > 0 and all(r[1] == 'lc' for r in rows))
busy = picked[0]                                # the connection we keep open

# 2) close the other two; their flows idle-expire -> flow-removed -> active decrements
for p in longs[1:]:
    p.terminate()
print('waiting for the 2 closed connections to expire (idle timeout)...')
deadline = time.time() + 90
while time.time() < deadline:
    if sh('ovs-ofctl -O OpenFlow13 dump-flows s1').count('priority=10') <= 2:
        break
    time.sleep(2)
time.sleep(1)

# 3) now only `busy` has a live connection; the next two requests must avoid it
before = len(csv_rows())
for _ in range(2):
    clients[1].cmd(f'curl -s --max-time 3 http://{VIP}/')
new = [r[4] for r in csv_rows()[before:]]
others = {s.name for s in servers} - {busy}
check(f'LC: with {busy} busy, next 2 requests go to the other two', len(new) == 2 and set(new) == others,
      f'busy={busy} picked={new}')

for p in longs:
    if p.poll() is None:
        p.terminate()
net.stop()
print('\nOVERALL:', 'ALL PASS' if all(ok_all) else 'SOME FAILED, paste this output')