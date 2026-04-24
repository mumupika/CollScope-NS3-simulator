# -*- coding: utf-8 -*-
# This is the file for generating clos topology.

if __name__ == '__main__':
    total_host = int(input("Please input total GPU counts: "))
    host = int(input("Please input GPU nums in RTSW: "))
    link_bw = input("Please input the link bandwidth: (Gbps)")
    link_delay =input("Please input the link delay: (ms)")
    
    assert total_host % host == 0, "total node should be integer dividable!"
    rtsw = total_host // host
    ctsw = rtsw
    atsw = ctsw // 2
    
    print(f"total_host: {total_host}; host: {host}; rtsw: {rtsw}, ctsw: {ctsw}, atsw: {atsw}")
    
    with open(f'./clos-{atsw}-{ctsw}-{rtsw}-{host}.txt', 'w') as f:
        total_node = total_host + rtsw + ctsw + atsw
        total_switch = total_node - total_host
        total_link = total_host + rtsw * ctsw + ctsw * atsw
        
        # print First line: total node #, switch node #, link #
        print(f"{total_node} {total_switch} {total_link}", file = f)
        print(f"{total_node} {total_switch} {total_link}")
        
        # print Second line: switch node IDs...
        for i in range(total_host, total_node):
            print(f"{i} ", file = f, end = "")
            print(f"{i} ", end = "")
        
        print("", file = f)
        print("")
        
        # print the detailed link.
        # connect host with each rtsw.
        i_rtsw = total_host - 1
        for i in range(total_host):
            if i % host == 0:
                i_rtsw += 1
            print(f"{i} {i_rtsw} {link_bw}Gbps {link_delay}ms 0", file = f)
            print(f"{i} {i_rtsw} {link_bw}Gbps {link_delay}ms 0")
        
        # connect rstw to ctsw.
        for i in range(total_host, total_host + rtsw):
            for j in range(total_host + rtsw, total_host + rtsw + ctsw):
                print(f"{i} {j} {link_bw}Gbps {link_delay}ms 0", file = f)
                print(f"{i} {j} {link_bw}Gbps {link_delay}ms 0")
        
        # connect ctsw to atsw.
        for i in range(total_host + rtsw, total_host + rtsw + ctsw):
            for j in range(total_host + rtsw + ctsw, total_host + rtsw + ctsw + atsw):
                print(f"{i} {j} {link_bw}Gbps {link_delay}ms 0", file = f)
                print(f"{i} {j} {link_bw}Gbps {link_delay}ms 0")
        
        print(f"First line: total node #, switch node #, link #\n\
Second line: switch node IDs...\n\
src0 dst0 rate delay error_rate\n\
src1 dst1 rate delay error_rate\n\
...", file = f)
        print(f"First line: total node #, switch node #, link #\n\
Second line: switch node IDs...\n\
src0 dst0 rate delay error_rate\n\
src1 dst1 rate delay error_rate\n\
...")