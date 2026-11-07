# computer-networks-project


### 1. Regression check (Round Robin must still pass). Controller with the default algorithm:
#### Terminal 1
cd ~/sdn-lb && ~/sdn-venv/bin/osken-manager lb_controller.py

#### Terminal 2
sudo mn -c && cd ~/sdn-lb && sudo python3 smoke_test.py


### 2. Least Connections test. Restart the controller in LC mode, so it starts with zero counts:
#### Terminal 1
cd ~/sdn-lb && LB_ALGO=lc ~/sdn-venv/bin/osken-manager lb_controller.py

#### Terminal 2
sudo mn -c && cd ~/sdn-lb && sudo python3 test_lc.py

### 3. F5 Load-aware with port statistics:
#### Terminal 1
cd ~/sdn-lb && LB_ALGO=load ~/sdn-venv/bin/osken-manager lb_controller.py

##### Terminal 2
sudo mn -c && cd ~/sdn-lb && sudo python3 test_load.py
