#!/usr/bin/env python3
"""Result graphs for the report (the proposal's matplotlib + pandas step).

    python3 plot_results.py                 # reads results.csv, writes plots/*.png
    python3 plot_results.py --servers 3     # only runs made with 3 servers (default: the most common count)
    python3 plot_results.py --rate 40       # only runs made at 40 req/s (default: all rates)

Bars are the mean over the repeated runs, error bars the standard deviation.
"""
import argparse
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import pandas as pd

NAMES = {'rr': 'Round Robin', 'lc': 'Least Connections', 'load': 'Load-aware'}
COLORS = {'rr': '#4c78a8', 'lc': '#f58518', 'load': '#54a24b'}

ap = argparse.ArgumentParser()
ap.add_argument('--csv', default='results.csv')
ap.add_argument('--servers', type=int)
ap.add_argument('--rate', type=int)
ap.add_argument('--out', default='plots')
args = ap.parse_args()

df = pd.read_csv(args.csv)
df = df[df['status'] == 'completed']
if args.rate:
    df = df[df['rate_req_s'] == args.rate]
n_srv = args.servers or int(df['servers'].mode()[0])
df = df[df['servers'] == n_srv]
if df.empty:
    raise SystemExit('no completed runs match these filters')
os.makedirs(args.out, exist_ok=True)
algos = [a for a in ('rr', 'lc', 'load') if a in set(df['algorithm'])]
title_suffix = '%d servers%s' % (n_srv, '' if not args.rate else ', %d req/s' % args.rate)


def bars(col, ylabel, fname, ylim=None):
    g = df.groupby('algorithm')[col].agg(['mean', 'std', 'count']).reindex(algos)
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar([NAMES[a] for a in algos], g['mean'], yerr=g['std'].fillna(0), capsize=5,
           color=[COLORS[a] for a in algos])
    for i, a in enumerate(algos):
        ax.text(i, g.loc[a, 'mean'], '%.2f (n=%d)' % (g.loc[a, 'mean'], g.loc[a, 'count']),
                ha='center', va='bottom', fontsize=9)
    ax.set_ylabel(ylabel)
    ax.set_title('%s (%s)' % (ylabel, title_suffix))
    if ylim:
        ax.set_ylim(*ylim)
    fig.tight_layout()
    fig.savefig(os.path.join(args.out, fname), dpi=160)
    plt.close(fig)


bars('avg_response_ms', 'Average response time (ms)', 'response_time.png')
bars('p95_response_ms', '95th percentile response time (ms)', 'p95_response_time.png')
bars('throughput_mbps', 'Aggregate throughput (Mbit/s)', 'throughput.png')
bars('jain', "Jain's index (connections)", 'jain_connections.png', (0, 1.1))
if 'jain_capacity' in df:
    bars('jain_capacity', "Jain's index (load / capacity)", 'jain_load_capacity.png', (0, 1.1))

# share of connections per server, mean over runs
rows = {}
for a in algos:
    tot = {}
    for s in df[df['algorithm'] == a]['assigned_per_server']:
        for kv in s.split('|'):
            k, v = kv.split(':')
            tot[k] = tot.get(k, 0) + int(v)
    allc = sum(tot.values())
    rows[NAMES[a]] = {k: 100.0 * v / allc for k, v in sorted(tot.items())}
share = pd.DataFrame(rows).T
ax = share.plot(kind='bar', stacked=True, figsize=(7, 4.5), rot=0)
ax.set_ylabel('Share of connections (%)')
ax.set_title('Connection distribution (%s)' % title_suffix)
ax.legend(title='server', bbox_to_anchor=(1.02, 1), loc='upper left')
plt.tight_layout()
plt.savefig(os.path.join(args.out, 'distribution.png'), dpi=160)

summary = df.groupby('algorithm')[['avg_response_ms', 'p95_response_ms', 'throughput_mbps', 'jain']
                                  + (['jain_capacity'] if 'jain_capacity' in df else [])].agg(['mean', 'std']).round(3)
summary.to_csv(os.path.join(args.out, 'summary.csv'))
print(summary.to_string())
print('\nwritten to', args.out + '/')