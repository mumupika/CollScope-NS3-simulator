#ifndef HYBRID_CC_H
#define HYBRID_CC_H

#include "ns3/application.h"
#include "ns3/event-id.h"
#include "ns3/ptr.h"
#include "ns3/ipv4-address.h"
#include "ns3/nstime.h"
#include "ns3/rdma.h"
#include "ns3/rdma-cc.h"
#include <vector>
#include <deque>

namespace ns3 {

/**
 * @brief HybridCC: Orchestrator for hybrid Data Parallel + Pipeline Parallel simulation.
 *
 * HybridCC holds an internal RdmaCC instance for DP group collective communication
 * (Allgather, ReduceScatter) and orchestrates multi-round Forward/Backward scheduling
 * with P2P inter-stage communication.
 *
 * Two scheduling modes:
 *  - EVENT_DRIVEN (0): Dynamic state machine, prioritize backward over forward,
 *                       with maxInflight limit.
 *  - STATIC_SCHEDULE (1): MegatronLM-style pre-computed schedule (warmup/1F1B/cooldown).
 */
class HybridCC : public Application {
public:
    static TypeId GetTypeId(void);
    HybridCC();
    virtual ~HybridCC();

    // ===== Configuration interfaces (called by Helper or main program) =====

    void SetRank(uint16_t rank);
    void SetLocal(Ipv4Address ip, uint16_t port);
    void SetDPGroupSize(uint32_t size);
    void SetNumPPStages(uint32_t stages);
    void SetNumChunks(uint16_t chunks);
    void SetTrainMode(bool train);
    void SetScheduleMode(uint8_t mode); // 0=event_driven, 1=static_schedule
    void SetMaxInflight(uint16_t max);
    void SetComputeDelay(Time tf, Time tb);
    void SetChunkSize(uint64_t fwdSize, uint64_t bwdSize);
    void SetControl(uint32_t win, uint64_t baseRtt, uint16_t pg);

    /**
     * @brief Configure DP group and create internal RdmaCC.
     * Called after all HybridCC instances are created and installed on nodes.
     *
     * @param allNodeIPs  IP addresses of all nodes (indexed by global rank)
     * @param allNodePorts  Ports of all nodes
     * @param ip2hybrid  Callback to find HybridCC by IP Ptr<HybridCC> ip_to_hybrid(Ipv4Address ip)
     * @param ip2rdma   Callback to find RdmaCC by IP (for DP group ring communication) Ptr<RdmaCC> ip_to_hybrid_rdma(Ipv4Address ip)
     */
    void SetApplication(
        const std::vector<Ipv4Address> &allNodeIPs,
        const std::vector<uint16_t> &allNodePorts,
        Callback<Ptr<HybridCC>, Ipv4Address> ip2hybrid,
        Callback<Ptr<RdmaCC>, Ipv4Address> ip2rdma);

    // ============= Query Interfaces: Get the IP/Port/Rank/bottom RdmaCC ===================
    Ipv4Address GetIP();
    uint16_t GetPort();
    uint16_t GetRank();
    Ptr<RdmaCC> GetRdmaCC();

    // ============= P2P step counting (called on sender leading rank) =============
    void OnP2PFwdStep(uint16_t chunkId);
    void OnP2PBwdStep(uint16_t chunkId);

    // ============= P2P completion callbacks =============
    // Called on each sender rank when its QP completes (via leading rank relay)
    void OnP2PFwdComplete(uint16_t chunkId);
    void OnP2PBwdComplete(uint16_t chunkId);
    // Called on each receiver rank when P2P data is received
    void OnP2PFwdRecv(uint16_t chunkId);
    void OnP2PBwdRecv(uint16_t chunkId);
    // Receiver-side mode-specific handlers (called by leading rank broadcast)
    void OnP2PFwdRecv_EventDriven(uint16_t chunkId);
    void OnP2PBwdRecv_EventDriven(uint16_t chunkId);
    void OnP2PFwdRecv_Static(uint16_t chunkId);
    void OnP2PBwdRecv_Static(uint16_t chunkId);

protected:
    virtual void DoDispose(void);

private:
    virtual void StartApplication(void);
    virtual void StopApplication(void);

    // ===== Phase tracking =====
    enum Phase {
        IDLE = 0,
        FWD_AG,
        FWD_COMPUTE,
        FWD_P2P,
        BWD_AG,
        BWD_COMPUTE_1,
        BWD_RS,
        BWD_COMPUTE_2,
        BWD_P2P,
        DONE
    };

    enum OpType {
        OP_FORWARD = 0,
        OP_BACKWARD = 1
    };

    // ===== Core operations (use m_dpRdmaCC internally) =====
    void Forward(uint16_t chunkId);
    void Backward(uint16_t chunkId);

    // RdmaCC completion callbacks
    void OnFwdAGComplete();
    void OnBwdAGComplete();
    void OnBwdRSComplete();

    // Compute delay handlers
    void OnFwdComputeDone();
    void OnBwdCompute1Done();
    void OnBwdCompute2Done();

    // P2P send (through RdmaCC)
    void SendP2PForward(uint16_t destRank, uint64_t size);
    void SendP2PBackward(uint16_t destRank, uint64_t size);
    void OnP2PSendComplete();
    uint16_t GetP2PFwdPartner();
    uint16_t GetP2PBwdPartner();

    // ===== Scheduling =====
    void ScheduleNext_EventDriven();
    void BuildSchedule();
    void ExecuteNextEntry();
    void ScheduleNext(); // Routes to event-driven or static

    // ===== Helpers =====
    void InitDerivedParams();
    uint16_t GetNextPort();

    // ===== Member variables =====

    // Internal RdmaCC for DP group collective communication
    Ptr<RdmaCC> m_dpRdmaCC;

    // Basic parameters
    uint16_t m_rank;
    Ipv4Address m_ip;
    uint16_t m_port;
    uint32_t m_dpGroupSize;
    uint32_t m_numPPStages;
    uint16_t m_numChunks;
    bool m_train;
    uint8_t m_scheduleMode;
    uint16_t m_maxInflight;

    // Compute delays
    Time m_tf;
    Time m_tb;

    // Data sizes
    uint64_t m_chunkSizeFwd;
    uint64_t m_chunkSizeBwd;

    // Congestion control
    uint32_t m_win;
    uint64_t m_baseRtt;
    uint16_t m_pg;

    // Derived parameters
    uint16_t m_ppStageIndex;
    uint16_t m_rankInGroup;
    bool m_isFirstStage;
    bool m_isLastStage;

    // All node info
    struct NodeInfo {
        Ipv4Address ip;
        uint16_t port;
    };
    std::vector<NodeInfo> m_nodes;

    // Callbacks
    Callback<Ptr<HybridCC>, Ipv4Address> m_ip2hybrid;

    // Current operation state
    Phase m_phase;
    uint16_t m_currentChunkId;
    uint16_t m_nextPort;

    // Event-driven mode state
    uint16_t m_fwdReadyCount;
    uint16_t m_bwdReadyCount;
    uint16_t m_fwdChunkIdx;
    uint16_t m_bwdChunkIdx;
    uint16_t m_inflightFwd;

    // Separate P2P receive completion counters for FWD and BWD
    uint16_t m_fwdP2PCompleteCount;
    uint16_t m_bwdP2PCompleteCount;

    // Operation serialization: true when an AG/RS or P2P send is active
    bool m_busy;

    // P2P send context (saved at send time, used in completion callback)
    uint16_t m_pendingP2PChunkId;
    bool m_pendingP2PIsForward;

    // Static schedule mode
    struct ScheduleEntry {
        OpType type;
        uint16_t chunkId;
    };
    std::deque<ScheduleEntry> m_schedule;
    uint32_t m_scheduleIdx;
    std::vector<bool> m_fwdP2PReceived;
    std::vector<bool> m_bwdP2PReceived;
};

} // namespace ns3

#endif /* HYBRID_CC_H */