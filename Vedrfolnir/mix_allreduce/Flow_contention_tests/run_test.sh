#!/bin/bash

cd ../../
config_files=("180us_1000us_3_case0" "180us_1000us_3_case1" "180us_1000us_3_case2" "180us_1000us_3_case3" "180us_1000us_3_case4" "180us_1000us_3_case5" "180us_1000us_3_case6" "180us_1000us_3_case7" "180us_1000us_3_case8" "180us_1000us_3_case9" "180us_1000us_3_case10" "180us_1000us_3_case11" "180us_1000us_3_case12" "180us_1000us_3_case13" "180us_1000us_3_case14" "180us_1000us_3_case15" "180us_1000us_3_case16" "180us_1000us_3_case17" "180us_1000us_3_case18" "180us_1000us_3_case19" "180us_1000us_3_case20" "180us_1000us_3_case21" "180us_1000us_3_case22" "180us_1000us_3_case23" "180us_1000us_3_case24" "180us_1000us_3_case25" "180us_1000us_3_case26" "180us_1000us_3_case27" "180us_1000us_3_case28" "180us_1000us_3_case29" "180us_1000us_3_case30" "180us_1000us_3_case31" "180us_1000us_3_case32" "180us_1000us_3_case33" "180us_1000us_3_case34" "180us_1000us_3_case35" "180us_1000us_3_case36" "180us_1000us_3_case37" "180us_1000us_3_case38" "180us_1000us_3_case39" "180us_1000us_3_case40" "180us_1000us_3_case41" "180us_1000us_3_case42" "180us_1000us_3_case43" "180us_1000us_3_case44" "180us_1000us_3_case45" "180us_1000us_3_case46" "180us_1000us_3_case47" "180us_1000us_3_case48" "180us_1000us_3_case49" "180us_1000us_3_case50" "180us_1000us_3_case51" "180us_1000us_3_case52" "180us_1000us_3_case53" "180us_1000us_3_case54" "180us_1000us_3_case55" "180us_1000us_3_case56" "180us_1000us_3_case57" "180us_1000us_3_case58" "180us_1000us_3_case59" )

printf "%s\n" "${config_files[@]}" | xargs -P 100 -I{} bash -c '
    file={}
    python ./waf --run "scratch/mix_allreduce mix_allreduce/Flow_contention_tests/config/config_${file}.txt"
    python3 mix_allreduce/Flow_contention_tests/graph.py "mix_allreduce/Flow_contention_tests/data/data_${file}"
'
