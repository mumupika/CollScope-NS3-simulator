docker run -d \
  --name ns3-vedrfolnir \
  --network host \
  -v /home/ubuntu/gs/Vedrfolnir:/root/Vedrfolnir \
  ns3-vedrfolnir \
  tail -f /dev/null