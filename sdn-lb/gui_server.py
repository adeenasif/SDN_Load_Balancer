#!/usr/bin/env python3
"""
gui_server.py - web GUI + traffic driver for the SDN load balancer.

ONE command starts everything (run it from this folder, as root because Mininet needs it):

    sudo python3 gui_server.py

It   1) starts the Ryu/os-ken controller (lb_controller.py) if none is running,
     2) builds the Mininet topology (topo.py: default 4 clients, 3 servers, 1 switch; the GUI can rebuild it
        with up to 10 hosts, e.g. 3 servers + 7 clients or 4 servers + 6 clients),
     3) serves the GUI on http://127.0.0.1:5000,
     4) turns the GUI's "Start traffic" button into REAL curl / iperf runs inside the Mininet
        client hosts, so you never have to type commands on the Mininet CLI.

Every number in the GUI comes from the controller (REST API on :8080) or from curl / iperf
output. Nothing is simulated.
"""
import argparse, csv, json, os, random, re, shutil, signal, socket, subprocess, sys, threading, time
import urllib.error, urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_PORT = int(os.environ.get('GUI_PORT', '5000'))
CTL_URL = os.environ.get('LB_URL', 'http://127.0.0.1:8080')
RESULTS_CSV = os.environ.get('GUI_RESULTS', os.path.join(HERE, 'results.csv'))
EVENTS_LOG = os.environ.get('GUI_EVENTS', os.path.join(HERE, 'events.log'))   # copy of the live event log

VIP = '10.0.0.100'
# Request-size workloads. 'uniform': every request fetches the same 40 KB page (every connection lasts about the same
# time, so Least Connections degenerates into Round Robin). 'mixed': 80% small pages and 20% large pages, in a fixed
# pseudo-random order that is identical for every algorithm, so connections have very different lifetimes.
SMALL_KB, LARGE_KB, LARGE_SHARE, WORKLOAD_SEED = 10, 250, 0.2, 2026
WORKLOADS = ('uniform', 'mixed')
PAYLOAD_KB = 40            # size of the page the clients download; big enough that a 2 Mbit/s link matters
PAGE = 'data.html'
CURL_TIMEOUT = 8           # seconds, per request
IPERF_SECONDS = 5          # length of the optional iperf throughput test
MAX_INFLIGHT = 150         # safety valve: never run more than this many curl processes at once
LIMITS = {'rate': (1, 60), 'duration': (10, 300)}   # the clients limit depends on the current topology
MAX_HOSTS = 10             # servers + clients (Sir: "max 10 nodes"); lower it to 9 if the switch must count too
SERVER_RANGE = (2, 6)
ALGO_NAMES = {'rr': 'Round Robin', 'lc': 'Least Connections', 'load': 'Load-aware'}
RUN_FIELDS = ['run_id', 'time', 'algorithm', 'workload', 'servers', 'clients', 'rate_req_s', 'duration_s', 'requests', 'ok', 'failed',
              'skipped', 'avg_response_ms', 'p95_response_ms', 'throughput_mbps', 'assigned_per_server', 'jain', 'jain_capacity', 'status']


def jain(xs):
    s = sum(xs)
    return (s * s) / (len(xs) * sum(x * x for x in xs)) if s else 1.0


def jain_cap(dist, caps):
    """Jain's index of connections per Mbit/s of link capacity (fair = proportional to capacity)."""
    return jain([dist[n] / caps.get(n, 10.0) for n in dist])


def default_cap(name):            # same rule as topo.py: srv3 has the slow 2 Mbit/s link
    return 2.0 if name == 'srv3' else 10.0


def port_open(port, host='127.0.0.1'):
    try:
        socket.create_connection((host, port), 0.5).close()
        return True
    except OSError:
        return False


class Gui:
    """All GUI state. `hosts` need .name and .popen() (Mininet hosts, or stand-ins in tests)."""

    def __init__(self, ctl_url=CTL_URL, vip=VIP, page=PAGE, servers=None, clients=None, results_csv=RESULTS_CSV,
                 events_log=EVENTS_LOG):
        self.ctl_url, self.vip, self.page = ctl_url, vip, page
        self.results_csv = results_csv
        self.events_log = events_log          # every live-log line is also appended here (see _log_line)
        self._ev_lock = threading.Lock()
        self._ev_file = None
        self._last_ev_seq = 0                 # newest controller assignment already written to the file
        self.lock = threading.RLock()
        self.phase, self.message = 'starting', 'Starting...'
        self.servers_info = servers or []     # [{'name','ip','capacity_mbps'}]
        self.clients_info = clients or []     # [{'name','ip'}]
        self.hosts = {}                       # name -> Mininet host
        self.net = None
        self.controller_proc = None
        self.view_workload = 'uniform'        # the results table compares runs that used the latest run's request sizes
        self.ctl, self.ctl_ok, self.ctl_err = None, False, 'not contacted yet'
        self.run, self.runs = None, []
        self.log_events = deque(maxlen=200)
        self.stop_event = threading.Event()
        self.thread = None
        self.procs = set()
        self.pool = ThreadPoolExecutor(max_workers=MAX_INFLIGHT)
        self.run_counter = 0
        self._load_runs()
        if self.runs:
            self.view_workload = self.runs[-1]['workload']

    # ------------------------------------------------------------------ helpers
    def say(self, kind, msg):
        now = time.time()
        with self.lock:
            self.log_events.appendleft({'ts': now, 't': time.strftime('%H:%M:%S'), 'kind': kind, 'msg': msg})
        self._log_line(now, kind, msg)

    # ---- live event log -> file (events.log): the same lines the dashboard shows, none dropped by the 60-line view
    def _log_line(self, ts, kind, msg):
        line = '%s  %-6s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts)), kind.upper(), msg)
        with self._ev_lock:
            try:
                if self._ev_file is None:
                    self._ev_file = open(self.events_log, 'a', encoding='utf-8', buffering=1)
                    self._ev_file.write('# --- GUI session started %s ---\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
                self._ev_file.write(line)
            except OSError:
                pass                      # a full disk or read-only folder must never stop the experiment

    def _log_assignments(self, snap):
        """Write controller assignment events not written yet (oldest first), formatted exactly like the live log."""
        events = snap.get('events') or []
        if not events:
            return
        if events[0].get('seq', 0) < self._last_ev_seq:      # controller was restarted: its counter started again
            self._last_ev_seq = 0
        names = {c['ip']: c['name'] for c in self.clients_info}
        for e in reversed(events):
            if e.get('seq', 0) <= self._last_ev_seq:
                continue
            self._last_ev_seq = e['seq']
            self._log_line(e.get('ts', time.time()), 'assign', '%s (%s) \u2192 %s \u00b7 %s' % (
                names.get(e.get('client_ip'), e.get('client_ip')), e.get('client'), e.get('srv'),
                ALGO_NAMES.get(e.get('algo'), e.get('algo'))))

    def set_phase(self, phase, message=''):
        with self.lock:
            self.phase, self.message = phase, message

    def ctl_get(self, path, timeout=1.5, raw=False):
        with urllib.request.urlopen(self.ctl_url + path, timeout=timeout) as r:
            data = r.read()
        return data if raw else json.loads(data)

    def ctl_post(self, path, body=None, timeout=3):
        req = urllib.request.Request(self.ctl_url + path, json.dumps(body or {}).encode(), method='POST',
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())

    def poll_controller(self):
        while True:
            try:
                snap = self.ctl_get('/lb/stats')
                with self.lock:
                    self.ctl, self.ctl_ok, self.ctl_err = snap, True, ''
                self._log_assignments(snap)
            except Exception as e:
                with self.lock:
                    self.ctl_ok, self.ctl_err = False, str(getattr(e, 'reason', e))
            time.sleep(0.5)

    # ------------------------------------------------------------------ results history
    def _load_runs(self):
        try:
            with open(self.results_csv, newline='') as f:
                for row in csv.DictReader(f):
                    try:
                        if 'assigned_per_server' in row:       # name:count|name:count
                            dist = {k: int(v) for k, v in (p.split(':') for p in row['assigned_per_server'].split('|') if p)}
                        else:                                  # older file: srv1, srv2, srv3 columns
                            dist = {s: int(row[s]) for s in ('srv1', 'srv2', 'srv3')}
                        self.runs.append({
                            'id': int(row['run_id']), 'time': row['time'], 'algorithm': row['algorithm'],
                            'workload': row.get('workload') or 'uniform',
                            'servers': len(dist), 'skipped': int(row.get('skipped') or 0),
                            'clients': int(row['clients']), 'rate': int(row['rate_req_s']),
                            'duration': int(row['duration_s']), 'sent': int(row['requests']),
                            'ok': int(row['ok']), 'failed': int(row['failed']),
                            'avg_ms': float(row['avg_response_ms']) if row['avg_response_ms'] else None,
                            'p95_ms': float(row['p95_response_ms']) if row['p95_response_ms'] else None,
                            'throughput_mbps': float(row['throughput_mbps']) if row['throughput_mbps'] else None,
                            'dist': dist,
                            'jain': float(row['jain']),
                            'jain_cap': (float(row['jain_capacity']) if row.get('jain_capacity')
                                         else round(jain_cap(dist, {n: default_cap(n) for n in dist}), 3)),
                            'status': row['status']})
                    except (KeyError, ValueError):
                        continue
        except OSError:
            pass
        self.run_counter = max([r['id'] for r in self.runs], default=0)
        self._migrate_csv()

    def _migrate_csv(self):
        """A results.csv written by an older version has different columns: keep it as .old and rewrite it."""
        try:
            with open(self.results_csv, newline='') as f:
                header = next(csv.reader(f), None)
        except OSError:
            return
        if header == RUN_FIELDS:
            return
        os.replace(self.results_csv, self.results_csv + '.old')
        for r in self.runs:
            self._save_run(r)

    def _save_run(self, r):
        new = not os.path.exists(self.results_csv)
        with open(self.results_csv, 'a', newline='') as f:
            w = csv.writer(f)
            if new:
                w.writerow(RUN_FIELDS)
            w.writerow([r['id'], r['time'], r['algorithm'], r['workload'], r['servers'], r['clients'], r['rate'], r['duration'],
                        r['sent'], r['ok'], r['failed'], r['skipped'],
                        '' if r['avg_ms'] is None else r['avg_ms'],
                        '' if r['p95_ms'] is None else r['p95_ms'],
                        '' if r['throughput_mbps'] is None else r['throughput_mbps'],
                        '|'.join('%s:%d' % kv for kv in r['dist'].items()), r['jain'], r['jain_cap'], r['status']])

    def clear_runs(self):
        with self.lock:
            if self.run and self.run['state'] in ('draining', 'running', 'measuring'):
                return False
            if os.path.exists(self.results_csv):
                os.replace(self.results_csv, self.results_csv + '.%d.bak' % int(time.time()))
            self.runs.clear()
            self.run = None
        self.say('info', 'Experiment results cleared (old file kept as a .bak copy)')
        return True

    def comparison(self):
        out = []
        names = [s['name'] for s in self.servers_info]
        for algo in ('rr', 'lc', 'load'):
            # only runs made with the current number of servers are comparable
            rs = [r for r in self.runs if r['algorithm'] == algo and r['status'] == 'completed'
                  and r['servers'] == len(names) and r['workload'] == self.view_workload]
            def mean(key):
                v = [r[key] for r in rs if r[key] is not None]
                return round(sum(v) / len(v), 3) if v else None
            tot = {s: sum(r['dist'].get(s, 0) for r in rs) for s in names}
            allc = sum(tot.values())
            out.append({'algorithm': algo, 'name': ALGO_NAMES[algo], 'runs': len(rs),
                        'avg_ms': mean('avg_ms'), 'p95_ms': mean('p95_ms'),
                        'throughput_mbps': mean('throughput_mbps'), 'jain': mean('jain'), 'jain_cap': mean('jain_cap'),
                        'share': {s: (round(100.0 * tot[s] / allc, 1) if allc else None) for s in tot}})
        return out

    # ------------------------------------------------------------------ state for the GUI
    def state(self):
        with self.lock:
            run = self.run
            r = None
            if run:
                n = run['rt_n']
                srt = sorted(run['rt_list'])
                series = self._series(run)
                r = {k: run[k] for k in ('id', 'state', 'algorithm', 'clients', 'rate', 'duration', 'iperf', 'sent', 'workload',
                                         'ok', 'failed', 'skipped', 'inflight', 'throughput_mbps', 'iperf_each', 'message', 'dist',
                                         'jain', 'jain_cap')}
                r['elapsed'] = round(run['elapsed'], 1)
                r['avg_ms'] = round(run['rt_sum'] / n, 1) if n else None
                r['p95_ms'] = round(srt[min(len(srt) - 1, int(0.95 * len(srt)))], 1) if srt else None
                r['per_server_ms'] = {s: (round(v[0] / v[1], 1) if v[1] else None) for s, v in run['per_srv'].items()}
                r['series'] = series
            ctl = self.ctl
            return {'now': time.time(), 'phase': self.phase, 'message': self.message,
                    'servers': self.servers_info, 'clients': self.clients_info, 'vip': self.vip,
                    'limits': self.limits(), 'topology': {'servers': len(self.servers_info), 'clients': len(self.clients_info),
                                                          'max_hosts': MAX_HOSTS, 'server_range': SERVER_RANGE},
                    'iperf_seconds': IPERF_SECONDS, 'payload_kb': PAYLOAD_KB,
                    'controller': {'ok': self.ctl_ok, 'error': self.ctl_err, 'data': ctl},
                    'run': r, 'runs': list(self.runs)[-30:], 'comparison': self.comparison(),
                    'log': list(self.log_events)[:80]}

    def limits(self):
        return dict(LIMITS, clients=(1, max(1, len(self.clients_info))))

    @staticmethod
    def _series(run):
        names = run['srv_names']
        if not run['buckets']:
            return dict({'t': [], 'all': []}, **{n: [] for n in names})
        last = max(run['buckets'])
        s = dict({'t': list(range(last + 1)), 'all': []}, **{n: [] for n in names})
        for sec in range(last + 1):
            b = run['buckets'].get(sec, {})
            tot = sum(v[0] for v in b.values())
            cnt = sum(v[1] for v in b.values())
            s['all'].append(round(tot / cnt, 1) if cnt else None)
            for name in names:
                v = b.get(name)
                s[name].append(round(v[0] / v[1], 1) if v and v[1] else None)
        return s

    # ------------------------------------------------------------------ actions from the GUI
    def busy(self):
        return bool(self.run and self.run['state'] in ('draining', 'running', 'measuring'))

    def set_algorithm(self, algo):
        if self.busy():
            return 409, {'ok': False, 'error': 'Stop the running traffic before changing the algorithm'}
        try:
            res = self.ctl_post('/lb/algorithm', {'algorithm': algo})
        except urllib.error.HTTPError as e:
            return 400, {'ok': False, 'error': 'controller rejected the algorithm (%s)' % e.code}
        except Exception as e:
            return 502, {'ok': False, 'error': 'controller unreachable: %s' % e}
        self.say('info', 'Algorithm set to %s (counters reset)' % ALGO_NAMES.get(algo, algo))
        return 200, res

    def reset_counters(self):
        if self.busy():
            return 409, {'ok': False, 'error': 'Stop the running traffic first'}
        try:
            self.ctl_post('/lb/reset')
        except Exception as e:
            return 502, {'ok': False, 'error': 'controller unreachable: %s' % e}
        self.say('info', 'Controller counters reset')
        return 200, {'ok': True}

    def start(self, rate, clients, duration, iperf, workload='uniform'):
        try:                                   # fresh read: the cached snapshot can be up to 0.5 s old
            fresh = self.ctl_get('/lb/stats')
            with self.lock:
                self.ctl, self.ctl_ok, self.ctl_err = fresh, True, ''
        except Exception:
            pass
        with self.lock:
            if self.phase != 'ready':
                return 409, {'ok': False, 'error': 'Topology is not ready yet: %s' % self.message}
            if self.busy():
                return 409, {'ok': False, 'error': 'A run is already in progress'}
            if not self.ctl_ok or not self.ctl or not self.ctl.get('switches'):
                return 409, {'ok': False, 'error': 'Controller is not connected to the switch'}
            self.run_counter += 1
            self.view_workload = workload
            self.run = {'id': self.run_counter, 'state': 'draining', 'algorithm': self.ctl['algorithm'], 'workload': workload,
                        'clients': clients, 'rate': rate, 'duration': duration, 'iperf': iperf,
                        'sent': 0, 'ok': 0, 'failed': 0, 'skipped': 0, 'inflight': 0,
                        'srv_names': [s['name'] for s in self.servers_info], 'elapsed': 0.0,
                        'rt_sum': 0.0, 'rt_n': 0, 'rt_list': [], 'per_srv': {}, 'buckets': {},
                        'throughput_mbps': None, 'iperf_each': {}, 'dist': None, 'jain': None, 'jain_cap': None, 'message': ''}
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run_traffic, args=(self.run,), daemon=True)
            self.thread.start()
        return 200, {'ok': True, 'run': self.run_counter}

    def stop(self):
        if not self.busy():
            return 200, {'ok': True}
        self.stop_event.set()
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            try:
                p.kill()
            except Exception:
                pass
        return 200, {'ok': True}

    # ------------------------------------------------------------------ the traffic driver
    def _wait_quiet(self, run):
        """Least-connections counts live flows and load-aware looks at traffic rate, so let the
        previous run's flows expire first. Otherwise one run would start with leftovers of another."""
        algo = run['algorithm']
        if algo not in ('lc', 'load'):
            return
        idle = (self.ctl or {}).get('idle_s', 30)
        deadline = time.time() + idle + 8
        while time.time() < deadline and not self.stop_event.is_set():
            try:
                c = self.ctl_get('/lb/stats')
            except Exception:
                c = self.ctl or {}
            servers = c.get('servers', [])
            if algo == 'lc' and sum(s['active'] for s in servers) == 0:
                return
            if algo == 'load' and sum(s['rate_mbps'] for s in servers) < 0.05:
                return
            with self.lock:
                run['message'] = 'Waiting for the previous flows to expire (%d left)...' % \
                                 sum(s['active'] for s in servers)
            time.sleep(0.5)

    def _run_traffic(self, run):
        try:
            self._run_traffic_inner(run)
        except Exception as e:
            self.say('error', 'Traffic driver crashed: %s' % e)
            with self.lock:
                run['state'], run['message'] = 'error', str(e)
        finally:
            with self.lock:
                for p in list(self.procs):
                    try:
                        p.kill()
                    except Exception:
                        pass
                self.procs.clear()

    def _run_traffic_inner(self, run):
        n_cl = run['clients']
        hosts = [self.hosts[c['name']] for c in self.clients_info[:n_cl]]
        self._wait_quiet(run)
        if self.stop_event.is_set():
            with self.lock:
                run['state'] = 'stopped'
            return
        try:
            self.ctl_post('/lb/reset')                     # every run starts from clean counters
        except Exception as e:
            raise RuntimeError('controller unreachable: %s' % e)
        with self.lock:
            run['state'], run['message'] = 'running', ''
        self.say('info', 'Traffic started: %d HTTP requests/s across %d client(s), %ds, %s' %
                 (run['rate'], n_cl, run['duration'], ALGO_NAMES.get(run['algorithm'])))

        interval = 1.0 / run['rate']
        t0 = time.monotonic()
        i = 0
        rng = random.Random(WORKLOAD_SEED)    # same size sequence in every run, whatever the algorithm
        while not self.stop_event.is_set():
            now = time.monotonic()
            run['elapsed'] = now - t0
            if run['elapsed'] >= run['duration']:
                break
            target = t0 + i * interval
            if target > now:
                self.stop_event.wait(min(target - now, 0.2))
                continue
            host = hosts[i % n_cl]
            i += 1
            page = self.page
            if run['workload'] == 'mixed':
                page = 'large.html' if rng.random() < LARGE_SHARE else 'small.html'
            with self.lock:
                if run['inflight'] >= MAX_INFLIGHT:      # the generator itself is saturated: not a network failure
                    run['skipped'] += 1
                    continue
                run['sent'] += 1
                run['inflight'] += 1
            self.pool.submit(self._request, run, host, t0, page)

        # let requests that are already in flight finish
        deadline = time.time() + CURL_TIMEOUT + 2
        while run['inflight'] > 0 and time.time() < deadline and not self.stop_event.is_set():
            time.sleep(0.1)
        run['elapsed'] = min(run['elapsed'], run['duration'])

        stopped = self.stop_event.is_set()
        time.sleep(0.6)                                    # let the controller finish counting
        try:
            snap = self.ctl_get('/lb/stats')
            dist = {s['name']: s['total'] for s in snap['servers']}
        except Exception:
            dist = {n: 0 for n in run['srv_names']}
        with self.lock:
            run['dist'] = dist
            run['jain'] = round(jain(list(dist.values())), 3)
            caps = {s['name']: s['capacity_mbps'] for s in self.servers_info}
            run['jain_cap'] = round(jain_cap(dist, caps), 3)

        if run['iperf'] and not stopped:
            with self.lock:
                run['state'] = 'measuring'
            self._iperf(run, hosts)
            stopped = self.stop_event.is_set()

        with self.lock:
            n, srt = run['rt_n'], sorted(run['rt_list'])
            rec = {'id': run['id'], 'time': time.strftime('%Y-%m-%d %H:%M:%S'), 'algorithm': run['algorithm'],
                   'workload': run['workload'],
                   'clients': run['clients'], 'rate': run['rate'], 'duration': run['duration'],
                   'sent': run['sent'], 'ok': run['ok'], 'failed': run['failed'], 'skipped': run['skipped'],
                   'servers': len(run['srv_names']),
                   'avg_ms': round(run['rt_sum'] / n, 1) if n else None,
                   'p95_ms': round(srt[min(len(srt) - 1, int(0.95 * len(srt)))], 1) if srt else None,
                   'throughput_mbps': run['throughput_mbps'], 'dist': dist, 'jain': run['jain'], 'jain_cap': run['jain_cap'],
                   'status': 'stopped' if stopped else 'completed'}
            run['state'] = rec['status'] if rec['status'] == 'stopped' else 'done'
            if run['sent'] > 0:
                self.runs.append(rec)
        if run['sent'] > 0:
            try:
                self._save_run(rec)
            except OSError as e:
                self.say('error', 'could not write %s: %s' % (self.results_csv, e))
        self.say('info', 'Traffic generation %s (%d ok, %d failed, Jain %.3f by connections, %.3f by load/capacity)' %
                 ('stopped' if stopped else 'finished', run['ok'], run['failed'], run['jain'], run['jain_cap']))
        if run['skipped']:
            self.say('error', '%d requests were not sent: more than %d curl processes were already running '
                              '(the load generator, not the network, was saturated)' % (run['skipped'], MAX_INFLIGHT))

    def _request(self, run, host, t0, page=None):
        cmd = ['curl', '-s', '--max-time', str(CURL_TIMEOUT), '-w', '\n@@%{time_total}@@%{http_code}',
               'http://%s/%s' % (self.vip, page or self.page)]
        srv, rt_ms, ok = None, None, False
        try:
            p = host.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            with self.lock:
                self.procs.add(p)
            try:
                out, err = p.communicate(timeout=CURL_TIMEOUT + 3)
            finally:
                with self.lock:
                    self.procs.discard(p)
            text = out.decode('utf-8', 'replace')
            body, _, tail = text.rpartition('\n@@')
            parts = tail.split('@@')
            code = parts[1].strip() if len(parts) > 1 else ''
            m = re.match(r'Hello from (srv\d+)', body)
            if code == '200' and m:
                srv, rt_ms, ok = m.group(1), float(parts[0]) * 1000.0, True
            elif not self.stop_event.is_set():
                reason = err.decode('utf-8', 'replace').strip() or ('http %s' % code)
                self.say('error', 'request from %s failed: %s' % (host.name, reason[:100]))
        except Exception as e:
            if not self.stop_event.is_set():
                self.say('error', 'request from %s failed: %s' % (host.name, e))
        with self.lock:
            run['inflight'] -= 1
            if ok:
                run['ok'] += 1
                run['rt_sum'] += rt_ms
                run['rt_n'] += 1
                run['rt_list'].append(rt_ms)
                v = run['per_srv'].setdefault(srv, [0.0, 0])
                v[0] += rt_ms
                v[1] += 1
                b = run['buckets'].setdefault(int(time.monotonic() - t0), {}).setdefault(srv, [0.0, 0])
                b[0] += rt_ms
                b[1] += 1
            elif not self.stop_event.is_set():
                run['failed'] += 1

    def _iperf(self, run, hosts):
        self.say('info', 'iperf throughput test: %d client(s) x %ds' % (len(hosts), IPERF_SECONDS))
        results = {}

        def one(host):
            cmd = ['iperf', '-c', self.vip, '-t', str(IPERF_SECONDS), '-f', 'm', '-y', 'C']
            try:
                p = host.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with self.lock:
                    self.procs.add(p)
                try:
                    out, err = p.communicate(timeout=IPERF_SECONDS + 12)
                finally:
                    with self.lock:
                        self.procs.discard(p)
                text = out.decode('utf-8', 'replace')
                mbps = None
                for line in reversed(text.strip().splitlines()):
                    parts = line.split(',')
                    if len(parts) >= 9 and parts[-1].strip().isdigit():
                        mbps = int(parts[-1]) / 1e6
                        break
                if mbps is None:
                    m = re.search(r'([\d.]+)\s+Mbits/sec', text)
                    mbps = float(m.group(1)) if m else None
                if mbps is None and not self.stop_event.is_set():
                    self.say('error', 'iperf from %s gave no result: %s' %
                             (host.name, (err.decode('utf-8', 'replace').strip() or 'no output')[:100]))
                results[host.name] = mbps
            except Exception as e:
                if not self.stop_event.is_set():
                    self.say('error', 'iperf from %s failed: %s' % (host.name, e))
                results[host.name] = None

        threads = [threading.Thread(target=one, args=(h,), daemon=True) for h in hosts]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        good = {k: round(v, 2) for k, v in results.items() if v is not None}
        with self.lock:
            run['iperf_each'] = good
            run['throughput_mbps'] = round(sum(good.values()), 2) if good else None
        if good:
            self.say('info', 'iperf aggregate throughput %.2f Mbit/s (%s)' %
                     (sum(good.values()), ', '.join('%s %.1f' % kv for kv in sorted(good.items()))))
        elif not self.stop_event.is_set():
            self.say('error', 'iperf measurement unavailable')

    # ------------------------------------------------------------------ network bring-up
    def bring_up(self, args):
        from mininet.clean import cleanup
        from mininet.log import setLogLevel
        setLogLevel('warning')
        self.set_phase('starting', 'Cleaning up old Mininet state...')
        self._kill_servers()
        try:
            cleanup()
        except Exception as e:
            print('cleanup warning:', e)
        if args.controller:
            self.controller_proc = ensure_controller()
        self._build_network(args.servers, args.clients)

    @staticmethod
    def _kill_servers():
        """Host processes started with '&' survive net.stop(); stop them so nothing piles up."""
        for pat in (['pkill', '-f', 'http.server 80'], ['pkill', '-x', 'iperf']):
            subprocess.run(pat, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _build_network(self, n_srv, n_cl):
        import topo
        self.set_phase('starting', 'Waiting for the controller REST API...')
        deadline = time.time() + 60
        while not self.ctl_ok and time.time() < deadline:
            time.sleep(0.5)
        if not self.ctl_ok:
            raise RuntimeError('the controller REST API (%s) is not answering. Is lb_controller.py running? '
                               '(osken-manager lb_controller.py)' % self.ctl_url)
        # tell the controller which servers exist and how fast their links are (needed by load-aware)
        pool = [{'name': 'srv%d' % i, 'ip': '10.0.0.%d' % i, 'mac': '00:00:00:00:00:0%d' % i, 'cap': topo.capacity(i)}
                for i in range(1, n_srv + 1)]
        self.ctl_post('/lb/topology', {'servers': pool})

        self.set_phase('starting', 'Building the Mininet topology (%d servers, %d clients)...' % (n_srv, n_cl))
        net, servers, clients = topo.build(n_srv, n_cl)
        net.start()
        self.net = net

        self.set_phase('waiting_controller', 'Waiting for the controller to see the switch...')
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                fresh = self.ctl_get('/lb/stats')
                if fresh.get('switches'):
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            raise RuntimeError('the switch never connected to the controller on 127.0.0.1:6653. '
                               'Is lb_controller.py running? (osken-manager lb_controller.py)')

        self.set_phase('starting', 'Starting web servers and iperf on the servers...')
        topo.start_servers(servers)
        for s in servers:                                   # the page that clients download
            head = 'Hello from %s\n' % s.name
            for fname, kb in ((self.page, PAYLOAD_KB), ('small.html', SMALL_KB), ('large.html', LARGE_KB)):
                with open('/tmp/%s/%s' % (s.name, fname), 'w') as f:
                    f.write(head + 'x' * max(0, kb * 1024 - len(head)))
        time.sleep(1)

        self.set_phase('starting', 'Letting the controller learn host ports (pingAll)...')
        for _ in range(3):
            net.pingAll()
            time.sleep(1)
            if self.ctl and self.ctl.get('ready'):
                break

        # IP() is only valid after net.start(); reading it earlier gave None ("null") in the GUI
        with self.lock:
            self.servers_info = [{'name': s.name, 'ip': s.IP(), 'capacity_mbps': topo.capacity(i + 1)}
                                 for i, s in enumerate(servers)]
            self.clients_info = [{'name': c.name, 'ip': c.IP()} for c in clients]
            self.hosts = {h.name: h for h in servers + clients}

        self.set_phase('starting', 'Self-test: c1 -> VIP ...')
        reply = ''
        for _ in range(3):
            p = clients[0].popen(['curl', '-s', '--max-time', '4', 'http://%s/%s' % (self.vip, self.page)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _err = p.communicate(timeout=10)
            reply = out.decode('utf-8', 'replace')[:40].strip()
            if reply.startswith('Hello from'):
                break
        try:
            self.ctl_post('/lb/reset')                      # the self-test must not count
        except Exception:
            pass
        if reply.startswith('Hello from'):
            self.say('info', 'Self-test OK, VIP answered: "%s"' % reply)
            self.set_phase('ready', '')
        else:
            self.say('error', 'Self-test FAILED: no reply from the VIP. Check the controller terminal.')
            self.set_phase('ready', 'Self-test failed: the VIP did not answer. You can still try Start traffic.')
        self.say('info', 'Topology ready: %d servers + %d clients (%d hosts), 1 switch' % (n_srv, n_cl, n_srv + n_cl))

    # ---- change the number of servers / clients from the GUI
    def set_topology(self, n_srv, n_cl):
        lo, hi = SERVER_RANGE
        if not lo <= n_srv <= hi:
            return 400, {'ok': False, 'error': 'servers must be %d..%d' % (lo, hi)}
        if not 1 <= n_cl <= MAX_HOSTS - n_srv:
            return 400, {'ok': False, 'error': 'with %d servers you can have 1..%d clients (max %d hosts)' %
                                                (n_srv, MAX_HOSTS - n_srv, MAX_HOSTS)}
        with self.lock:
            if self.busy():
                return 409, {'ok': False, 'error': 'Stop the running traffic first'}
            if self.phase != 'ready':
                return 409, {'ok': False, 'error': 'The topology is still starting'}
            if (n_srv, n_cl) == (len(self.servers_info), len(self.clients_info)):
                return 200, {'ok': True, 'unchanged': True}
            self.phase, self.message = 'rebuilding', 'Rebuilding the topology: %d servers, %d clients...' % (n_srv, n_cl)
            self.run = None                                 # its charts belong to the old topology
        threading.Thread(target=self._rebuild, args=(n_srv, n_cl), daemon=True).start()
        return 200, {'ok': True}

    def _rebuild(self, n_srv, n_cl):
        from mininet.clean import cleanup
        try:
            self._kill_servers()
            try:
                self.net.stop()
            except Exception as e:
                print('stop warning:', e)
            try:
                cleanup()
            except Exception as e:
                print('cleanup warning:', e)
            time.sleep(1.5)            # let the controller see the old switch disconnect before the new one appears
            self._build_network(n_srv, n_cl)
        except Exception as e:
            print('[gui] REBUILD FAILED:', e)
            self.say('error', 'Rebuild failed: %s' % e)
            self.set_phase('error', str(e))

    def shutdown(self):
        self.stop_event.set()
        self._kill_servers()
        try:
            self.net.stop()
        except Exception:
            pass
        cp = getattr(self, 'controller_proc', None)
        if cp and cp.poll() is None:
            cp.terminate()


# ---------------------------------------------------------------------- controller launcher
def ensure_controller():
    """Start lb_controller.py unless something already listens on the OpenFlow port 6653."""
    if port_open(6653):
        print('[gui] a controller is already listening on :6653, using it')
        return None
    env = dict(os.environ)
    env.setdefault('LB_IDLE', '2')       # flows expire 2 s after a connection ends, so Least Connections counts open connections
    env.setdefault('LB_POLL', '1')       # port statistics every second
    script = os.path.join(HERE, 'lb_controller.py')
    candidates = []
    exe = shutil.which('osken-manager')
    if exe:
        candidates.append([exe, script])
    candidates.append([sys.executable, '-c', 'from os_ken.cmd.manager import main; main()', script])
    user = os.environ.get('SUDO_USER')
    if user and user != 'root':      # os-ken installed for the normal user only
        keep = ','.join(k for k in env if k.startswith('LB_'))
        candidates.append(['sudo', '-u', user, '-H', '--preserve-env=' + keep, 'bash', '-lc',
                           'cd "%s" && osken-manager lb_controller.py' % HERE])
    log = open(os.path.join(HERE, 'controller.log'), 'w')
    for cmd in candidates:
        print('[gui] starting controller:', ' '.join(cmd))
        p = subprocess.Popen(cmd, cwd=HERE, env=env, stdout=log, stderr=subprocess.STDOUT)
        for _ in range(20):
            time.sleep(0.5)
            if p.poll() is not None:
                break
            if port_open(6653):
                print('[gui] controller is up (log: controller.log)')
                return p
        if p.poll() is None:
            p.terminate()
    print('[gui] could not start the controller automatically (see controller.log).\n'
          '      Open another terminal and run:  osken-manager lb_controller.py\n'
          '      The GUI will pick it up as soon as it is running.')
    return None


# ---------------------------------------------------------------------- HTTP server
GUI = None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json', extra=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split('?')[0]
        try:
            if path in ('/', '/index.html'):
                with open(os.path.join(HERE, 'dashboard.html'), 'rb') as f:
                    return self._send(200, f.read(), 'text/html; charset=utf-8')
            if path == '/api/state':
                return self._send(200, GUI.state())
            if path == '/api/export/runs.csv':
                with open(GUI.results_csv, 'rb') as f:
                    data = f.read()
                return self._send(200, data, 'text/csv; charset=utf-8',
                                  {'Content-Disposition': 'attachment; filename="experiment_results.csv"'})
            if path == '/api/export/assignments.csv':
                data = GUI.ctl_get('/lb/assignments.csv', raw=True)
                return self._send(200, data, 'text/csv; charset=utf-8',
                                  {'Content-Disposition': 'attachment; filename="assignments.csv"'})
        except FileNotFoundError:
            return self._send(404, {'error': 'nothing to export yet - run an experiment first'})
        except Exception as e:
            return self._send(502, {'error': str(e)})
        self._send(404, {'error': 'not found'})

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n)) if n else {}
            path = self.path
            if path == '/api/algorithm':
                return self._send(*GUI.set_algorithm(body.get('algorithm')))
            if path == '/api/reset':
                return self._send(*GUI.reset_counters())
            if path == '/api/topology':
                return self._send(*GUI.set_topology(int(body.get('servers')), int(body.get('clients'))))
            if path == '/api/stop':
                return self._send(*GUI.stop())
            if path == '/api/clear':
                ok = GUI.clear_runs()
                return self._send(200 if ok else 409, {'ok': ok})
            if path == '/api/start':
                vals = {}
                lim = GUI.limits()
                for k in ('rate', 'clients', 'duration'):
                    lo, hi = lim[k]
                    vals[k] = int(body.get(k))
                    if not lo <= vals[k] <= hi:
                        return self._send(400, {'ok': False, 'error': '%s must be %d..%d' % (k, lo, hi)})
                workload = body.get('workload', 'uniform')
                if workload not in WORKLOADS:
                    return self._send(400, {'ok': False, 'error': 'workload must be one of %s' % (WORKLOADS,)})
                return self._send(*GUI.start(vals['rate'], vals['clients'], vals['duration'],
                                             bool(body.get('iperf', True)), workload))
        except (TypeError, ValueError):
            return self._send(400, {'ok': False, 'error': 'bad request'})
        except Exception as e:
            return self._send(500, {'ok': False, 'error': str(e)})
        self._send(404, {'error': 'not found'})


def serve(gui, port=GUI_PORT):
    global GUI
    GUI = gui
    srv = ThreadingHTTPServer(('0.0.0.0', port), Handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Thread(target=gui.poll_controller, daemon=True).start()
    return srv


def main():
    ap = argparse.ArgumentParser(description='SDN load balancer GUI + Mininet traffic driver')
    ap.add_argument('--no-controller', dest='controller', action='store_false',
                    help='do not start lb_controller.py automatically (you run it yourself)')
    ap.add_argument('--servers', type=int, default=3, help='initial number of servers (2..6, default 3)')
    ap.add_argument('--clients', type=int, default=4, help='initial number of clients (default 4; servers+clients <= 10)')
    args = ap.parse_args()
    if not SERVER_RANGE[0] <= args.servers <= SERVER_RANGE[1] or not 1 <= args.clients <= MAX_HOSTS - args.servers:
        sys.exit('need %d..%d servers and 1..%d clients (max %d hosts)' %
                 (SERVER_RANGE[0], SERVER_RANGE[1], MAX_HOSTS - args.servers, MAX_HOSTS))
    if os.geteuid() != 0:
        sys.exit('Mininet needs root. Run:  sudo python3 gui_server.py')

    gui = Gui()
    gui.controller_proc = None
    def _term(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, _term)
    serve(gui)
    print('[gui] open  http://127.0.0.1:%d  in the browser (it shows progress while the topology starts)' % GUI_PORT)
    try:
        try:
            gui.bring_up(args)
            print('[gui] ready. Press Ctrl-C here to stop everything.')
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print('[gui] START-UP FAILED:', e)
            gui.set_phase('error', str(e))
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n[gui] stopping...')
    finally:
        gui.shutdown()


if __name__ == '__main__':
    main()
