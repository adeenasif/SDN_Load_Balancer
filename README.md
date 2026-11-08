# SDN Load Balancer (CS3001 Computer Networks Project)
 
An SDN-based layer-4 load balancer built with **Mininet**, **Open vSwitch** and an **OS-Ken** controller
(OpenFlow 1.3), plus a web dashboard to run experiments and compare three server-selection algorithms.


## 1. How it works
 
Clients send HTTP requests to one virtual IP (VIP `10.0.0.100`). They never see the real servers.
 
1. The first packet (SYN) of every new connection has no flow rule, so the switch sends it to the controller.
2. The controller picks a server with the active algorithm and installs two flow rules in the switch:
   client -> VIP is rewritten to the chosen server's IP/MAC, and the server's reply is rewritten so it appears to come
   from the VIP.
3. All other packets of that connection are forwarded by the switch alone, with no controller involvement.
4. The controller also answers ARP requests for the VIP, counts connections (flow-removed events when flows idle out)
   and polls port statistics from the switch (used by Load-aware).
   
### Topology (default)
4 clients (`c1-c4`, 10.0.0.11-14), 3 servers (`srv1-srv3`, 10.0.0.1-3), 1 Open vSwitch `s1`, 1 remote controller.
`srv3` has a **2 Mbit/s** link, the other servers **10 Mbit/s** (unequal capacity on purpose).
The GUI can rebuild the network with other sizes: 2-6 servers, 1+ clients, **max 10 hosts** (e.g. 3+7 or 4+6).

 
### Algorithms
| Algorithm | Picks the server with... |
|---|---|
| Round Robin (`rr`) | the next server in turn |
| Least Connections (`lc`) | the fewest active connections (flow-removed events decrement the count) |
| Load-aware (`load`) | the lowest load from port statistics |
 
Load-aware has two modes (`LB_LOAD_MODE`):
- `norm` (default): load = measured traffic / link capacity, plus an estimate for new connections that the stats do not
  show yet. Fair for unequal links.
- `raw`: the proposal's literal F5, the plain traffic rate in Mbit/s. Kept as a baseline for comparison.

### Metrics
Response time (avg, p95) from curl, aggregate throughput from iperf, and **Jain's fairness index** reported two ways:
by connection count (even split) and by load / link capacity (the fair measure when links differ).


## 2. Files
| File | Purpose |
|---|---|
| `lb_controller.py` | OS-Ken controller: VIP/ARP, algorithms, flow rules, port-stats polling, REST API (port 8080) |
| `topo.py` | Mininet topology (`build(n_servers, n_clients)`), can also run on its own |
| `gui_server.py` | Builds the Mininet network, generates traffic (curl/iperf), serves the dashboard (port 5000), stores results |
| `dashboard.html` | The web GUI |
| `plot_results.py` | Report graphs from `results.csv` (pandas + matplotlib) |
| `smoke_test.py`, `test_lc.py`, `test_load.py` | Command-line tests for Round Robin, Least Connections and Load-aware |
| `results.csv`, `assignments.csv` | Generated: one row per experiment run / per connection assignment |


## 3. Requirements
Ubuntu with Mininet, Open vSwitch, curl and iperf (iperf2), Python 3, and OS-Ken in a virtualenv:
 
```bash
sudo apt update
sudo apt install mininet openvswitch-switch curl iperf python3-matplotlib python3-pandas -y
python3 -m venv ~/sdn-venv && ~/sdn-venv/bin/pip install os-ken
```
The commands below assume the project is in `~/sdn-lb`.


## 4. Command-line tests (no GUI)
Each test needs its own controller start so the counters begin at zero. Use two terminals.
Do not run these while the GUI is running.
 
**Round Robin (smoke test)**
```bash
# Terminal 1
cd ~/sdn-lb && ~/sdn-venv/bin/osken-manager lb_controller.py
# Terminal 2
sudo mn -c && cd ~/sdn-lb && sudo python3 smoke_test.py
```
 
**Least Connections**
```bash
# Terminal 1
cd ~/sdn-lb && LB_ALGO=lc ~/sdn-venv/bin/osken-manager lb_controller.py
# Terminal 2
sudo mn -c && cd ~/sdn-lb && sudo python3 test_lc.py
```
 
**Load-aware (port statistics)**
```bash
# Terminal 1
cd ~/sdn-lb && LB_ALGO=load ~/sdn-venv/bin/osken-manager lb_controller.py
# Terminal 2
sudo mn -c && cd ~/sdn-lb && sudo python3 test_load.py
```
Each script should end with `ALL PASS`.


## 5. Running the GUI
The GUI builds its own Mininet network, so **do not run `topo.py` at the same time**. Start the controller yourself
so you can choose its settings, then start the GUI with `--no-controller`.
 
```bash
# Terminal 1: controller, normal (capacity-normalised) mode
cd ~/sdn-lb && LB_POLL=1 LB_IDLE=10 ~/sdn-venv/bin/osken-manager lb_controller.py
 
# Terminal 1 alternative: raw mode (literal F5, for the comparison)
cd ~/sdn-lb && LB_LOAD_MODE=raw LB_POLL=1 LB_IDLE=10 ~/sdn-venv/bin/osken-manager lb_controller.py
 
# Terminal 2
sudo mn -c && cd ~/sdn-lb && sudo python3 gui_server.py --no-controller
```
Open **http://127.0.0.1:5000**. Optional start size: `--servers 4 --clients 6`.
To switch between normal and raw mode, stop the controller (Ctrl+C), start it again with the other setting,
and restart the GUI. The results table does not record the mode, so click **Clear results** between modes.
 
Quick check from the Mininet CLI (`sudo python3 topo.py` without the GUI): `c1 sh -c 'for i in $(seq 12); do curl -s 10.0.0.100; done'`
prints `Hello from srv1/srv2/srv3` in rotation under Round Robin.


### Using the dashboard
1. Choose servers and clients, then **Apply topology** (rebuild takes about 10-20 s).
2. Choose an algorithm and click **Apply algorithm** (**Reset counters** zeroes the statistics).
3. Set request rate, active clients and duration, then **Start traffic** (curl requests, then an iperf phase).
4. Watch per-server utilisation, connection distribution, response time and the live event log.
5. Repeat each algorithm 3+ times. The **Experiment results** table compares the means over repeated runs
   (only runs with the current number of servers). Use **Export results CSV / Export assignments CSV** for the report.

## 6. Graphs for the report
```bash
cd ~/sdn-lb && python3 plot_results.py                 # all completed runs, most common server count
cd ~/sdn-lb && python3 plot_results.py --servers 3 --rate 16
```
Writes PNGs and `summary.csv` to `plots/` (mean with standard deviation over repeated runs).
 d ~/sdn-lb && sudo python3 plot_results.py

