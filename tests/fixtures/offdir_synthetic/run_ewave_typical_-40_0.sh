#!/bin/sh
# SYNTHETIC fixture -- shape copied from an official GUI-generated run script,
# every value is an obvious placeholder. See README.md in this directory.

ewave --nogui -m --workDir=. --emssTechFile='/fake/pdk/apps/ewave/ewaveinterface/process/typical/typical_v2/ptxt_enc/FAKEPDK_atypical_typical_V1.0_encrypted_package.ptxt' --gds=MY_CELL.gds --top=MY_CELL --cadencePins=1  --labelDepth=0 -p 'P000=MY_GND' -p 'P001=MY_INN' -p 'P002=MY_INP' -p 'P003=my_bias' -p 'P004=my_tune' -i P001 -i P002 -i P003 -i P004 --multiSweep=adaptive,0:0.1:40 --viaMergeSpace=0.4 -e 0.4 -d 0.4 --equalCurrent --viaMode=1 --relativeTolerance=1e-05 --relativeCurrentTolerance=0.001 --sparamImpedance=50 --parallel=20 --sparam=MY_CELL --corner=typical --temperature=-40.0 --key=000000 |sed -r 's/\x1B[[0-9;]*m//g'
