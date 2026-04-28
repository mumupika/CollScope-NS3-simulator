/* -*- Mode:C++; c-file-style:"gnu"; indent-tabs-mode:nil; -*- */
#include "hybrid-cc-helper.h"
#include "ns3/names.h"
#include "ns3/uinteger.h"
#include "ns3/ipv4-address.h"

namespace ns3 {

HybridCCHelper::HybridCCHelper() {
    m_factory.SetTypeId(HybridCC::GetTypeId());
}

HybridCCHelper::HybridCCHelper(uint16_t rank, Ipv4Address ip, uint16_t port) {
    m_factory.SetTypeId(HybridCC::GetTypeId());
    m_factory.Set("RankID", UintegerValue(rank));
    m_factory.Set("IP", Ipv4AddressValue(ip));
    m_factory.Set("Port", UintegerValue(port));
}

void HybridCCHelper::SetAttribute(std::string name, const AttributeValue &value) {
    m_factory.Set(name, value);
}

Ptr<Application> HybridCCHelper::Install(Ptr<Node> node) {
    Ptr<HybridCC> app = m_factory.Create<HybridCC>();
    node->AddApplication(app);
    return app;
}

void HybridCCHelper::ConfigureApplications(
    const ApplicationContainer &apps,
    const std::vector<Ipv4Address> &allNodeIPs,
    const std::vector<uint16_t> &allNodePorts,
    Callback<Ptr<HybridCC>, Ipv4Address> ip2hybrid,
    Callback<Ptr<RdmaCC>, Ipv4Address> ip2rdma) {
    for (uint32_t i = 0; i < apps.GetN(); i++) {
        Ptr<HybridCC> hybrid = DynamicCast<HybridCC>(apps.Get(i));
        if (hybrid) {
            hybrid->SetApplication(allNodeIPs, allNodePorts, ip2hybrid, ip2rdma);
        }
    }
}

} // namespace ns3