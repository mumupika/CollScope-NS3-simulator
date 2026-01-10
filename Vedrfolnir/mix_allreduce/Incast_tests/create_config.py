# -*- coding: utf-8 -*-
import os

# threshold_list = ["0"]
threshold_list = ["180"]
epoch_time_list = ["1000"]
step_detect_times_list = ['3']

file = open("config_tp.txt", "r")
lines = file.readlines()
file.close()

if not os.path.exists("data"):
    os.makedirs("data")
if not os.path.exists("data_result"):
    os.makedirs("data_result")
if not os.path.exists("config"):
    os.makedirs("config")

def read_data(filename):
    with open(filename, "r") as f:
        content = f.read()
    
    data = []
    current_group = []
    
    for line in content.splitlines():
        if line.startswith("Group"):
            if current_group:
                data.append(current_group)
                current_group = []
        elif line.startswith("    flow"):
            unit_str = line.split(": ")[1].strip("[]")
            unit_data = [eval(item) for item in unit_str.split(", ")]
            current_group.append(unit_data)
    
    if current_group:
        data.append(current_group)
    
    return tuple(data)
cases = read_data("Incast_case.txt")

config_files = []

for threshold in threshold_list:
    for epoch_time in epoch_time_list:
        for detect_times in step_detect_times_list:
            for i, case in enumerate(cases):
                file_str = threshold+"us_"+epoch_time+"us_"+detect_times+"_case"+str(i)
                config_files.append(file_str)
                config_file = open("config/config_"+file_str+".txt", "w")
                for line in lines:
                    config_file.write(line)
                config_file.write("\n")
                data_path = "mix_allreduce/Incast_tests/data/data_"+file_str

                config_file.write("DIR "+ data_path + "\n")
                config_file.write("FCT_OUTPUT_FILE "+ data_path + "/fct.txt" +"\n")
                config_file.write("PFC_OUTPUT_FILE "+ data_path + "/pfc.txt" +"\n")
                config_file.write("AGENT_THRESHOLD "+threshold+"\n")
                config_file.write("AUTO_AGENT_THRESHOLD "+threshold+"\n") # remove when testing Hawkeye
                config_file.write("EPOCH_TIME "+epoch_time+"000\n")
                config_file.write("STEP_DETECT_TIMES "+detect_times+"\n")
                config_file.write("FLOW_FILE "+ data_path + "/flow.txt" +"\n")
                
                config_file.close()
                dir = "data/data_"+file_str
                if not os.path.exists(dir):
                    # print("create dir")
                    os.makedirs(dir)

                flow_file = open(dir+"/flow.txt", "w")
                flow_file.write(f'{len(case)}\n')

                for flow in case:                  
                    flow_file.write(f"{flow[0]} {flow[1]} 3 10001 {flow[2]} {flow[3]}\n")
                flow_file.close()

#create run_test.sh
file = open("run_test.sh", "w")
file.write("#!/bin/bash\n")
file.write("\n")
file.write("cd ../../\n")
file.write("config_files=(")

for file_str in config_files:
    file.write("\""+file_str+"\" ")
file.write(")\n")
file.write("\n")

file.write("printf \"%s\\n\" \"${config_files[@]}\" | xargs -P 100 -I{} bash -c '\n")
file.write("    file={}\n")
file.write("    python ./waf --run \"scratch/mix_allreduce mix_allreduce/Incast_tests/config/config_${file}.txt\"\n")
file.write("    python3 mix_allreduce/Incast_tests/graph.py \"mix_allreduce/Incast_tests/data/data_${file}\"\n")
file.write("'\n")
file.close()