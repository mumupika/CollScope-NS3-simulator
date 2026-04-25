/* -*- Mode:C++; c-file-style:"gnu"; indent-tabs-mode:nil; -*- */
#ifndef HYBRID_CC_HELPER_H
#define HYBRID_CC_HELPER_H

#include <stdint.h>
#include "ns3/application-container.h"
#include "ns3/node-container.h"
#include "ns3/object-factory.h"
#include "ns3/ipv4-address.h"
#include "ns3/hybrid-cc.h"

namespace ns3 {

/**
 * @brief The HybridCCHelper is the helper class for set the attribute of the hybrid-cc application
 *        And Configurate all the apps on each node for the following schedule.
 */
class HybridCCHelper {
public:
    /**
     * @brief Construct a new Hybrid CC Helper object.
     *        Default Constructor.
     * 
     */
    HybridCCHelper();

    /**
     * @brief Construct a new Hybrid CC Helper object with informations.
     * 
     * @param rank The current Node rank ID.
     * @param ip   The current Node IP.
     * @param port The flow port.
     */
    HybridCCHelper(uint16_t rank, Ipv4Address ip, uint16_t port);

    /**
     * @brief Set the Attribute object. This is going to set `hybrid-cc` application attributes.
     * 
     * @param name  Attribute name, supported names and their value types:
     * 
     *   | name             | value type            | description                              |
     *   |------------------|-----------------------|------------------------------------------|
     *   | "RankID"         | UintegerValue         | Global rank ID                           |
     *   | "DPGroupSize"    | UintegerValue         | Data parallel group size                 |
     *   | "NumPPStages"    | UintegerValue         | Number of pipeline parallel stages       |
     *   | "NumChunks"      | UintegerValue         | Number of micro-batches (chunks)         |
     *   | "TrainMode"      | BooleanValue          | Enable backward pass or forward only     |
     *   | "ScheduleMode"   | UintegerValue         | 0=event-driven, 1=static (MegatronLM)    |
     *   | "MaxInflight"    | UintegerValue         | Max inflight forward chunks per stage    |
     *   | "ComputeTimeFwd" | TimeValue (ms)        | Forward compute delay                    |
     *   | "ComputeTimeBwd" | TimeValue (ms)        | Backward compute delay                   |
     *   | "ChunkSizeFwd"   | UintegerValue (bytes) | Forward chunk data size                  |
     *   | "ChunkSizeBwd"   | UintegerValue (bytes) | Backward chunk data size                 |
     *   | "IP"             | Ipv4AddressValue      | Source IP address                        |
     *   | "Port"           | UintegerValue         | Source port number                       |
     *   | "Window"         | UintegerValue         | Bound of on-the-fly packets              |
     *   | "BaseRtt"        | UintegerValue (ns)    | Base RTT for congestion control          |
     *   | "PriorityGroup"  | UintegerValue         | Priority group                           |
     * 
     * @param value The attribute value, type must match the corresponding name.
     */
    void SetAttribute(std::string name, const AttributeValue &value);

    /**
     * @brief Install the HybridCC into the node. Return the pointer.
     * 
     * @param node The node which waits for installing.
     * @return Ptr<HybridCC> The HybridCC Application which installed.
     */
    Ptr<Application> Install(Ptr<Node> node);

    /**
     * @brief The Configuration of the Applications. Here we need to clarify
     *        What steps we need to take and get the pipeline ready.
     * 
     * @param apps              The hybrid-cc applications container.
     * @param allNodeIPs        The agent Node IPs that participate in hybrid CC.
     * @param allNodePorts      The Ports that agent Node uses.
     * @param ip2hybrid         Callback function: Ptr<HybridCC> ip_to_hybrid(Ipv4Address ip);
     * @param ip2rdma           Callback function: Ptr<RdmaCC> ip_to_hybrid_rdma(Ipv4Address ip);
     */
    void ConfigureApplications(
        const ApplicationContainer &apps,
        const std::vector<Ipv4Address> &allNodeIPs,
        const std::vector<uint16_t> &allNodePorts,
        Callback<Ptr<HybridCC>, Ipv4Address> ip2hybrid,
        Callback<Ptr<RdmaCC>, Ipv4Address> ip2rdma,
        Callback<Ptr<RdmaCC>, Ipv4Address> ip2ppRdma);

private:
    ObjectFactory m_factory;
};

} // namespace ns3

#endif /* HYBRID_CC_HELPER_H */