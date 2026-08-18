#!/bin/sh
# SYNTHETIC fixture -- shape copied from the official remote submit script.

dsub -A fake_account -q fake_queue -R "cpu=20;mem=100000" -I ./run_ewave_typical_-40_0.sh 2>&1 |tee run_ewave_typical_-40_0.log &
