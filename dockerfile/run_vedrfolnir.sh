docker run -d \
  --name ns3-vedrfolnir \
  --network host \
  --privileged \
  -v /home/ubuntu/gs/Vedrfolnir:/root/Vedrfolnir \
  -v /lib/modules:/lib/modules:ro \
  -v /usr/src:/usr/src:ro \
  -v /sys/kernel/debug:/sys/kernel/debug \
  -v /sys/fs/cgroup:/sys/fs/cgroup \
  -v /sys/fs/bpf:/sys/fs/bpf \
  ns3-vedrfolnir \
  tail -f /dev/null