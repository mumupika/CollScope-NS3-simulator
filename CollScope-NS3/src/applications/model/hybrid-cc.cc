#include "hybrid-cc.h"
#include "ns3/log.h"
#include "ns3/uinteger.h"
#include "ns3/boolean.h"
#include "ns3/simulator.h"
#include "ns3/rdma-driver.h"
#include "ns3/rdma-hw.h"

namespace ns3 {

NS_LOG_COMPONENT_DEFINE("HybridCC");
NS_OBJECT_ENSURE_REGISTERED(HybridCC);

TypeId
HybridCC::GetTypeId(void) {
    static TypeId tid = TypeId("ns3::HybridCC")
                            .SetParent<Application>()
                            .AddConstructor<HybridCC>()
                            .AddAttribute("RankID",
                                          "Global rank ID",
                                          UintegerValue(0),
                                          MakeUintegerAccessor(&HybridCC::m_rank),
                                          MakeUintegerChecker<uint16_t>())
                            .AddAttribute("DPGroupSize",
                                          "Data parallel group size",
                                          UintegerValue(4),
                                          MakeUintegerAccessor(&HybridCC::m_dpGroupSize),
                                          MakeUintegerChecker<uint32_t>())
                            .AddAttribute("NumPPStages",
                                          "Number of pipeline parallel stages",
                                          UintegerValue(4),
                                          MakeUintegerAccessor(&HybridCC::m_numPPStages),
                                          MakeUintegerChecker<uint32_t>())
                            .AddAttribute("NumChunks",
                                          "Number of micro-batches (chunks)",
                                          UintegerValue(1),
                                          MakeUintegerAccessor(&HybridCC::m_numChunks),
                                          MakeUintegerChecker<uint16_t>())
                            .AddAttribute("TrainMode",
                                          "Enable backward pass (train) or forward only (test)",
                                          BooleanValue(true),
                                          MakeBooleanAccessor(&HybridCC::m_train),
                                          MakeBooleanChecker())
                            .AddAttribute("ScheduleMode",
                                          "0=event-driven, 1=static schedule (MegatronLM)",
                                          UintegerValue(0),
                                          MakeUintegerAccessor(&HybridCC::m_scheduleMode),
                                          MakeUintegerChecker<uint8_t>())
                            .AddAttribute("MaxInflight",
                                          "Maximum inflight forward chunks per stage",
                                          UintegerValue(100),
                                          MakeUintegerAccessor(&HybridCC::m_maxInflight),
                                          MakeUintegerChecker<uint16_t>())
                            .AddAttribute("ComputeTimeFwd",
                                          "Forward compute delay (ms)",
                                          TimeValue(MilliSeconds(0)),
                                          MakeTimeAccessor(&HybridCC::m_tf),
                                          MakeTimeChecker())
                            .AddAttribute("ComputeTimeBwd",
                                          "Backward compute delay (ms)",
                                          TimeValue(MilliSeconds(0)),
                                          MakeTimeAccessor(&HybridCC::m_tb),
                                          MakeTimeChecker())
                            .AddAttribute("ChunkSizeFwd",
                                          "Forward chunk data size (bytes)",
                                          UintegerValue(0),
                                          MakeUintegerAccessor(&HybridCC::m_chunkSizeFwd),
                                          MakeUintegerChecker<uint64_t>())
                            .AddAttribute("ChunkSizeBwd",
                                          "Backward chunk data size (bytes)",
                                          UintegerValue(0),
                                          MakeUintegerAccessor(&HybridCC::m_chunkSizeBwd),
                                          MakeUintegerChecker<uint64_t>())
                            .AddAttribute("IP",
                                          "Source IP",
                                          Ipv4AddressValue("0.0.0.0"),
                                          MakeIpv4AddressAccessor(&HybridCC::m_ip),
                                          MakeIpv4AddressChecker())
                            .AddAttribute("Port",
                                          "Source Port",
                                          UintegerValue(10000),
                                          MakeUintegerAccessor(&HybridCC::m_port),
                                          MakeUintegerChecker<uint16_t>())
                            .AddAttribute("Window",
                                          "Bound of on-the-fly packets",
                                          UintegerValue(0),
                                          MakeUintegerAccessor(&HybridCC::m_win),
                                          MakeUintegerChecker<uint32_t>())
                            .AddAttribute("BaseRtt",
                                          "Base RTT",
                                          UintegerValue(0),
                                          MakeUintegerAccessor(&HybridCC::m_baseRtt),
                                          MakeUintegerChecker<uint64_t>())
                            .AddAttribute("PriorityGroup",
                                          "Priority group",
                                          UintegerValue(3),
                                          MakeUintegerAccessor(&HybridCC::m_pg),
                                          MakeUintegerChecker<uint16_t>());
    return tid;
}

HybridCC::HybridCC() :
    m_rank(0),
    m_port(10000),
    m_dpGroupSize(4),
    m_numPPStages(4),
    m_numChunks(1),
    m_train(true),
    m_scheduleMode(0),
    m_maxInflight(100),
    m_tf(MilliSeconds(0)),
    m_tb(MilliSeconds(0)),
    m_chunkSizeFwd(0),
    m_chunkSizeBwd(0),
    m_win(0),
    m_baseRtt(0),
    m_pg(3),
    m_phase(IDLE),
    m_currentChunkId(0),
    m_nextPort(10001),
    m_fwdReadyCount(0),
    m_bwdReadyCount(0),
    m_fwdChunkIdx(0),
    m_bwdChunkIdx(0),
    m_inflightFwd(0),
    m_fwdP2PCompleteCount(0),
    m_bwdP2PCompleteCount(0),
    m_busy(false),
    m_pendingP2PChunkId(0),
    m_pendingP2PIsForward(false),
    m_scheduleIdx(0) {
}

HybridCC::~HybridCC() {
}

// ===== Configuration setters =====

void HybridCC::SetRank(uint16_t rank) {
    m_rank = rank;
}
void HybridCC::SetLocal(Ipv4Address ip, uint16_t port) {
    m_ip = ip;
    m_port = port;
}
void HybridCC::SetDPGroupSize(uint32_t size) {
    m_dpGroupSize = size;
}
void HybridCC::SetNumPPStages(uint32_t stages) {
    m_numPPStages = stages;
}
void HybridCC::SetNumChunks(uint16_t chunks) {
    m_numChunks = chunks;
}
void HybridCC::SetTrainMode(bool train) {
    m_train = train;
}
void HybridCC::SetScheduleMode(uint8_t mode) {
    m_scheduleMode = mode;
}
void HybridCC::SetMaxInflight(uint16_t max) {
    m_maxInflight = max;
}
void HybridCC::SetComputeDelay(Time tf, Time tb) {
    m_tf = tf;
    m_tb = tb;
}
void HybridCC::SetChunkSize(uint64_t fwdSize, uint64_t bwdSize) {
    m_chunkSizeFwd = fwdSize;
    m_chunkSizeBwd = bwdSize;
}
void HybridCC::SetControl(uint32_t win, uint64_t baseRtt, uint16_t pg) {
    m_win = win;
    m_baseRtt = baseRtt;
    m_pg = pg;
}

Ipv4Address HybridCC::GetIP() {
    return m_ip;
}
uint16_t HybridCC::GetPort() {
    return m_port;
}
uint16_t HybridCC::GetRank() {
    return m_rank;
}
Ptr<RdmaCC> HybridCC::GetRdmaCC() {
    return m_dpRdmaCC;
}

uint16_t HybridCC::GetNextPort() {
    return m_nextPort++;
}

uint16_t HybridCC::GetP2PFwdPartner() {
    return m_rank + m_dpGroupSize;
}

uint16_t HybridCC::GetP2PBwdPartner() {
    return m_rank - m_dpGroupSize;
}

// ===== SetApplication =====

void HybridCC::SetApplication(
    const std::vector<Ipv4Address> &allNodeIPs,
    const std::vector<uint16_t> &allNodePorts,
    Callback<Ptr<HybridCC>, Ipv4Address> ip2hybrid,
    Callback<Ptr<RdmaCC>, Ipv4Address> ip2rdma) {
    NS_ASSERT(allNodeIPs.size() == allNodePorts.size());
    NS_ASSERT_MSG(m_dpGroupSize * m_numPPStages == allNodeIPs.size(),
                  "dp_group_size(" << m_dpGroupSize << ") * num_pp_stages(" << m_numPPStages
                                   << ") != total_nodes(" << allNodeIPs.size() << ")");

    // Save all node info
    for (size_t i = 0; i < allNodeIPs.size(); i++) {
        NodeInfo ni;
        ni.ip = allNodeIPs[i];
        ni.port = allNodePorts[i];
        m_nodes.push_back(ni);
    }
    m_ip2hybrid = ip2hybrid;

    // Compute derived parameters
    InitDerivedParams();

    // Create internal RdmaCC for DP group communication
    m_dpRdmaCC = CreateObject<RdmaCC>();
    m_dpRdmaCC->SetRank(m_rankInGroup);
    m_dpRdmaCC->SetLocal(m_ip, m_port);
    m_dpRdmaCC->SetAlg(1); // ring algorithm
    m_dpRdmaCC->SetControl(m_win, m_baseRtt, m_pg);

    // Add DP group members (same PP stage)
    uint16_t groupBase = m_ppStageIndex * m_dpGroupSize;
    for (uint32_t i = 0; i < m_dpGroupSize; i++) {
        uint16_t globalRank = groupBase + i;
        m_dpRdmaCC->AddCommGroup(m_nodes[globalRank].ip, m_nodes[globalRank].port);
    }

    // Set IP2APP callback for RdmaCC ring communication
    m_dpRdmaCC->SetIP2APPCb(ip2rdma);

    // Install m_dpRdmaCC on this node
    m_dpRdmaCC->SetStartTime(Seconds(1e9));
    uint32_t app_index = GetNode()->AddApplication(m_dpRdmaCC);

    // Initialize P2P tracking arrays
    m_fwdP2PReceived.resize(m_numChunks, false);
    m_bwdP2PReceived.resize(m_numChunks, false);

    NS_LOG_INFO("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank
                     << " ppStage=" << m_ppStageIndex
                     << " rankInGroup=" << m_rankInGroup
                     << " isFirstStage=" << m_isFirstStage
                     << " isLastStage=" << m_isLastStage);
}

void HybridCC::InitDerivedParams() {
    m_ppStageIndex = m_rank / m_dpGroupSize;
    m_rankInGroup = m_rank % m_dpGroupSize;
    m_isFirstStage = (m_ppStageIndex == 0);
    m_isLastStage = (m_ppStageIndex == m_numPPStages - 1);
}

// ===== Application lifecycle =====

void HybridCC::StartApplication(void) {
    NS_LOG_INFO("t=" << Simulator::Now().GetNanoSeconds() << "ns, StartApplication rank=" << m_rank
                     << " phase=" << m_phase
                     << " scheduleMode=" << (int)m_scheduleMode);

    m_fwdP2PCompleteCount = 0;
    m_bwdP2PCompleteCount = 0;
    m_busy = false;

    if (m_scheduleMode == 1) {
        BuildSchedule();
        Simulator::Schedule(Seconds(0), &HybridCC::ExecuteNextEntry, this);
    } else {
        if (m_isFirstStage) {
            m_fwdReadyCount = m_numChunks;
        }
        m_bwdReadyCount = 0;
        m_fwdChunkIdx = 0;
        m_bwdChunkIdx = 0;
        m_inflightFwd = 0;
        Simulator::Schedule(Seconds(0), &HybridCC::ScheduleNext_EventDriven, this);
    }
}

void HybridCC::StopApplication(void) {
    NS_LOG_INFO("t=" << Simulator::Now().GetNanoSeconds() << "ns, StopApplication rank=" << m_rank);
}

void HybridCC::DoDispose(void) {
    Application::DoDispose();
}

// ===== Forward operation: AG → tf → [P2P] =====

void HybridCC::Forward(uint16_t chunkId) {
    NS_LOG_INFO("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " Forward chunk=" << chunkId);
    m_currentChunkId = chunkId;
    m_phase = FWD_AG;
    m_busy = true;

    m_dpRdmaCC->Reset();
    uint64_t agSize = m_chunkSizeFwd / m_dpGroupSize;
    m_dpRdmaCC->SetChunk(agSize, 1);
    m_dpRdmaCC->SetCompletionCallback(
        MakeCallback(&HybridCC::OnFwdAGComplete, this));
    m_dpRdmaCC->Allgather();
    Simulator::Schedule(Seconds(0), &RdmaCC::StartApplication, m_dpRdmaCC);
}

void HybridCC::OnFwdAGComplete() {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " FwdAG complete chunk=" << m_currentChunkId);
    m_phase = FWD_COMPUTE;
    Simulator::Schedule(m_tf, &HybridCC::OnFwdComputeDone, this);
}

void HybridCC::OnFwdComputeDone() {
    if (m_isLastStage) {
        // Last stage: no FWD P2P needed, forward is complete
        m_inflightFwd--;
        if (m_train) {
            if (m_scheduleMode == 0) {
                m_bwdReadyCount++;
            } else {
                m_bwdP2PReceived[m_currentChunkId] = true;
            }
        }
        m_busy = false;
        Simulator::Schedule(Seconds(0), &HybridCC::ScheduleNext, this);
    } else {
        m_phase = FWD_P2P;
        SendP2PForward(GetP2PFwdPartner(), m_chunkSizeFwd);
    }
}

// ===== Backward operation: AG → tb → RS → tb → [P2P] =====

void HybridCC::Backward(uint16_t chunkId) {
    NS_LOG_INFO("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " Backward chunk=" << chunkId);
    m_currentChunkId = chunkId;
    m_phase = BWD_AG;
    m_busy = true;

    m_dpRdmaCC->Reset();
    uint64_t agSize = m_chunkSizeBwd / m_dpGroupSize;
    m_dpRdmaCC->SetChunk(agSize, 1);
    m_dpRdmaCC->SetCompletionCallback(
        MakeCallback(&HybridCC::OnBwdAGComplete, this));
    m_dpRdmaCC->Allgather();
    Simulator::Schedule(Seconds(0), &RdmaCC::StartApplication, m_dpRdmaCC);
}

void HybridCC::OnBwdAGComplete() {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " BwdAG complete chunk=" << m_currentChunkId);
    m_phase = BWD_COMPUTE_1;
    Simulator::Schedule(m_tb, &HybridCC::OnBwdCompute1Done, this);
}

void HybridCC::OnBwdCompute1Done() {
    m_phase = BWD_RS;

    m_dpRdmaCC->Reset();
    uint64_t rsSize = m_chunkSizeBwd / m_dpGroupSize;
    m_dpRdmaCC->SetChunk(rsSize, 1);
    m_dpRdmaCC->SetCompletionCallback(
        MakeCallback(&HybridCC::OnBwdRSComplete, this));
    m_dpRdmaCC->ReduceScatter();
    Simulator::Schedule(Seconds(0), &RdmaCC::StartApplication, m_dpRdmaCC);
}

void HybridCC::OnBwdRSComplete() {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " BwdRS complete chunk=" << m_currentChunkId);
    m_phase = BWD_COMPUTE_2;
    Simulator::Schedule(m_tb, &HybridCC::OnBwdCompute2Done, this);
}

void HybridCC::OnBwdCompute2Done() {
    if (m_isFirstStage) {
        // First stage: no BWD P2P needed, backward is complete
        m_busy = false;
        Simulator::Schedule(Seconds(0), &HybridCC::ScheduleNext, this);
    } else {
        m_phase = BWD_P2P;
        SendP2PBackward(GetP2PBwdPartner(), m_chunkSizeBwd);
    }
}

// ===== P2P communication (real RDMA) =====

void HybridCC::SendP2PForward(uint16_t destRank, uint64_t size) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " SendP2PFWD to rank=" << destRank
                      << " size=" << size << " chunk=" << m_currentChunkId);

    // Save context for completion callback
    m_pendingP2PChunkId = m_currentChunkId;
    m_pendingP2PIsForward = true;

    // Get partner info: rank[i] in current stage sends to rank[i] in next stage
    uint16_t partnerRank = m_rank + m_dpGroupSize; // same as destRank
    Ipv4Address partnerIp = m_nodes[partnerRank].ip;
    uint16_t partnerPort = m_nodes[partnerRank].port;

    // Issue real RDMA write via this node's RdmaDriver
    Ptr<RdmaDriver> rdma = GetNode()->GetObject<RdmaDriver>();
    NS_ASSERT_MSG(rdma != nullptr, "RdmaDriver not found on node for rank " << m_rank);

    rdma->AddQueuePair(size, m_pg, m_ip, partnerIp,
                       GetNextPort(), partnerPort,
                       m_win, m_baseRtt,
                       MakeCallback(&HybridCC::OnP2PSendComplete, this));
    // m_busy remains true until QP completes
}

void HybridCC::SendP2PBackward(uint16_t destRank, uint64_t size) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " SendP2PBWD to rank=" << destRank
                      << " size=" << size << " chunk=" << m_currentChunkId);

    // Save context for completion callback
    m_pendingP2PChunkId = m_currentChunkId;
    m_pendingP2PIsForward = false;

    // Get partner info: rank[i] in current stage sends to rank[i] in previous stage
    uint16_t partnerRank = m_rank - m_dpGroupSize; // same as destRank
    Ipv4Address partnerIp = m_nodes[partnerRank].ip;
    uint16_t partnerPort = m_nodes[partnerRank].port;

    // Issue real RDMA write via this node's RdmaDriver
    Ptr<RdmaDriver> rdma = GetNode()->GetObject<RdmaDriver>();
    NS_ASSERT_MSG(rdma != nullptr, "RdmaDriver not found on node for rank " << m_rank);

    rdma->AddQueuePair(size, m_pg, m_ip, partnerIp,
                       GetNextPort(), partnerPort,
                       m_win, m_baseRtt,
                       MakeCallback(&HybridCC::OnP2PSendComplete, this));
    // m_busy remains true until QP completes
}

void HybridCC::OnP2PSendComplete() {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PSendComplete"
                      << " chunk=" << m_pendingP2PChunkId
                      << " isForward=" << m_pendingP2PIsForward);

    // Notify the sender group's leading rank to count this completion
    uint16_t senderLeadingRank = m_ppStageIndex * m_dpGroupSize;
    Ptr<HybridCC> leadingHybrid = m_ip2hybrid(m_nodes[senderLeadingRank].ip);

    if (m_pendingP2PIsForward) {
        Simulator::Schedule(Seconds(0), &HybridCC::OnP2PFwdStep,
                            leadingHybrid, m_pendingP2PChunkId);
    } else {
        Simulator::Schedule(Seconds(0), &HybridCC::OnP2PBwdStep,
                            leadingHybrid, m_pendingP2PChunkId);
    }
}

// ===== P2P step counting (on sender leading rank) =====

void HybridCC::OnP2PFwdStep(uint16_t chunkId) {
    m_fwdP2PCompleteCount++;
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PFwdStep chunk=" << chunkId
                      << " count=" << m_fwdP2PCompleteCount << "/" << m_dpGroupSize);

    if (m_fwdP2PCompleteCount >= m_dpGroupSize) {
        m_fwdP2PCompleteCount = 0;

        // 1) Notify all sender group members to proceed
        uint16_t senderGroupBase = m_ppStageIndex * m_dpGroupSize;
        for (uint32_t i = 0; i < m_dpGroupSize; i++) {
            Ptr<HybridCC> s = m_ip2hybrid(m_nodes[senderGroupBase + i].ip);
            Simulator::Schedule(Seconds(0), &HybridCC::OnP2PFwdComplete, s, chunkId);
        }

        // 2) Notify receiver group's leading rank to broadcast to its members
        uint16_t recvLeadingRank = (m_ppStageIndex + 1) * m_dpGroupSize;
        Ptr<HybridCC> recvLeading = m_ip2hybrid(m_nodes[recvLeadingRank].ip);
        Simulator::Schedule(Seconds(0), &HybridCC::OnP2PFwdRecv, recvLeading, chunkId);
    }
}

void HybridCC::OnP2PBwdStep(uint16_t chunkId) {
    m_bwdP2PCompleteCount++;
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PBwdStep chunk=" << chunkId
                      << " count=" << m_bwdP2PCompleteCount << "/" << m_dpGroupSize);

    if (m_bwdP2PCompleteCount >= m_dpGroupSize) {
        m_bwdP2PCompleteCount = 0;

        // 1) Notify all sender group members to proceed
        uint16_t senderGroupBase = m_ppStageIndex * m_dpGroupSize;
        for (uint32_t i = 0; i < m_dpGroupSize; i++) {
            Ptr<HybridCC> s = m_ip2hybrid(m_nodes[senderGroupBase + i].ip);
            Simulator::Schedule(Seconds(0), &HybridCC::OnP2PBwdComplete, s, chunkId);
        }

        // 2) Notify receiver group's leading rank to broadcast to its members
        uint16_t recvLeadingRank = (m_ppStageIndex - 1) * m_dpGroupSize;
        Ptr<HybridCC> recvLeading = m_ip2hybrid(m_nodes[recvLeadingRank].ip);
        Simulator::Schedule(Seconds(0), &HybridCC::OnP2PBwdRecv, recvLeading, chunkId);
    }
}

// ===== P2P completion callbacks =====

// Called on each sender rank: QP done, proceed to next schedule
void HybridCC::OnP2PFwdComplete(uint16_t chunkId) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PFwdComplete chunk=" << chunkId);
    m_inflightFwd--;
    m_busy = false;
    Simulator::Schedule(Seconds(0), &HybridCC::ScheduleNext, this);
}

void HybridCC::OnP2PBwdComplete(uint16_t chunkId) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PBwdComplete chunk=" << chunkId);
    m_busy = false;
    Simulator::Schedule(Seconds(0), &HybridCC::ScheduleNext, this);
}

// Called on receiver leading rank: broadcast to all receiver group members
void HybridCC::OnP2PFwdRecv(uint16_t chunkId) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PFwdRecv (leading) chunk=" << chunkId);
    // Notify all ranks in this (receiver) stage
    uint16_t recvGroupBase = m_ppStageIndex * m_dpGroupSize;
    for (uint32_t i = 0; i < m_dpGroupSize; i++) {
        Ptr<HybridCC> r = m_ip2hybrid(m_nodes[recvGroupBase + i].ip);
        if (m_scheduleMode == 0) {
            Simulator::Schedule(Seconds(0), &HybridCC::OnP2PFwdRecv_EventDriven, r, chunkId);
        } else {
            Simulator::Schedule(Seconds(0), &HybridCC::OnP2PFwdRecv_Static, r, chunkId);
        }
    }
}

void HybridCC::OnP2PBwdRecv(uint16_t chunkId) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PBwdRecv (leading) chunk=" << chunkId);
    // Notify all ranks in this (receiver) stage
    uint16_t recvGroupBase = m_ppStageIndex * m_dpGroupSize;
    for (uint32_t i = 0; i < m_dpGroupSize; i++) {
        Ptr<HybridCC> r = m_ip2hybrid(m_nodes[recvGroupBase + i].ip);
        if (m_scheduleMode == 0) {
            Simulator::Schedule(Seconds(0), &HybridCC::OnP2PBwdRecv_EventDriven, r, chunkId);
        } else {
            Simulator::Schedule(Seconds(0), &HybridCC::OnP2PBwdRecv_Static, r, chunkId);
        }
    }
}

// Receiver-side handlers for event-driven mode
void HybridCC::OnP2PFwdRecv_EventDriven(uint16_t chunkId) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PFwdRecv_EventDriven chunk=" << chunkId);
    m_fwdReadyCount++;
    Simulator::Schedule(Seconds(0), &HybridCC::ScheduleNext_EventDriven, this);
}

void HybridCC::OnP2PBwdRecv_EventDriven(uint16_t chunkId) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PBwdRecv_EventDriven chunk=" << chunkId);
    m_bwdReadyCount++;
    Simulator::Schedule(Seconds(0), &HybridCC::ScheduleNext_EventDriven, this);
}

// Receiver-side handlers for static schedule mode
void HybridCC::OnP2PFwdRecv_Static(uint16_t chunkId) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PFwdRecv_Static chunk=" << chunkId);
    m_fwdP2PReceived[chunkId] = true;
    Simulator::Schedule(Seconds(0), &HybridCC::ExecuteNextEntry, this);
}

void HybridCC::OnP2PBwdRecv_Static(uint16_t chunkId) {
    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " OnP2PBwdRecv_Static chunk=" << chunkId);
    m_bwdP2PReceived[chunkId] = true;
    Simulator::Schedule(Seconds(0), &HybridCC::ExecuteNextEntry, this);
}

// ===== Scheduling =====

void HybridCC::ScheduleNext() {
    if (m_scheduleMode == 0) {
        Simulator::Schedule(Seconds(0), &HybridCC::ScheduleNext_EventDriven, this);
    } else {
        Simulator::Schedule(Seconds(0), &HybridCC::ExecuteNextEntry, this);
    }
}

// ----- Event-driven scheduling -----

void HybridCC::ScheduleNext_EventDriven() {
    // If an operation (AG/RS/compute/P2P) is still in progress, cannot start new operations
    if (m_busy) {
        NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " busy, waiting"
                          << " fwdReady=" << m_fwdReadyCount
                          << " bwdReady=" << m_bwdReadyCount);
        return;
    }

    bool allFwdDone = (m_fwdChunkIdx >= m_numChunks);
    bool allBwdDone = (!m_train) || (m_bwdChunkIdx >= m_numChunks);

    if (allFwdDone && allBwdDone) {
        m_phase = DONE;
        NS_LOG_INFO("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " ALL DONE");
        return;
    }

    // Priority: backward > forward
    if (m_train && m_bwdReadyCount > 0 && m_bwdChunkIdx < m_numChunks) {
        m_bwdReadyCount--;
        Backward(m_bwdChunkIdx++);
        return;
    }

    if (m_fwdReadyCount > 0 && m_fwdChunkIdx < m_numChunks
        && m_inflightFwd < m_maxInflight) {
        m_fwdReadyCount--;
        m_inflightFwd++;
        Forward(m_fwdChunkIdx++);
        return;
    }

    NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " idle "
                      << " fwdReady=" << m_fwdReadyCount
                      << " bwdReady=" << m_bwdReadyCount
                      << " inflight=" << m_inflightFwd
                      << " fwdIdx=" << m_fwdChunkIdx
                      << " bwdIdx=" << m_bwdChunkIdx);
}

// ----- Static schedule (MegatronLM) -----

void HybridCC::BuildSchedule() {
    m_schedule.clear();

    uint16_t num_warmup = std::min({m_maxInflight,
                                    m_numChunks,
                                    static_cast<uint16_t>(m_numPPStages - 1 - m_ppStageIndex)});

    uint16_t num_1f1b = m_numChunks - num_warmup;

    NS_LOG_INFO("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank
                     << " BuildSchedule: warmup=" << num_warmup
                     << " steady_1f1b=" << num_1f1b
                     << " cooldown=" << num_warmup);

    for (uint16_t i = 0; i < num_warmup; i++) {
        ScheduleEntry e;
        e.type = OP_FORWARD;
        e.chunkId = i;
        m_schedule.push_back(e);
    }

    for (uint16_t i = 0; i < num_1f1b; i++) {
        ScheduleEntry ef;
        ef.type = OP_FORWARD;
        ef.chunkId = static_cast<uint16_t>(num_warmup + i);
        m_schedule.push_back(ef);

        if (m_train) {
            ScheduleEntry eb;
            eb.type = OP_BACKWARD;
            eb.chunkId = i;
            m_schedule.push_back(eb);
        }
    }

    for (uint16_t i = 0; i < num_warmup; i++) {
        if (m_train) {
            ScheduleEntry eb;
            eb.type = OP_BACKWARD;
            eb.chunkId = static_cast<uint16_t>(num_1f1b + i);
            m_schedule.push_back(eb);
        }
    }

    m_scheduleIdx = 0;

    std::cout << "Rank " << m_rank << " (stage " << m_ppStageIndex << ") schedule: ";
    for (size_t i = 0; i < m_schedule.size(); i++) {
        std::cout << (m_schedule[i].type == OP_FORWARD ? "F" : "B")
                  << m_schedule[i].chunkId << " ";
    }
    std::cout << std::endl;
}

void HybridCC::ExecuteNextEntry() {
    if (m_scheduleIdx >= m_schedule.size()) {
        m_phase = DONE;
        NS_LOG_INFO("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank << " ALL DONE (static)");
        return;
    }

    ScheduleEntry &entry = m_schedule[m_scheduleIdx];

    if (entry.type == OP_FORWARD) {
        if (!m_isFirstStage && !m_fwdP2PReceived[entry.chunkId]) {
            NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank
                              << " waiting for FwdP2P chunk=" << entry.chunkId);
            return;
        }
        m_scheduleIdx++;
        Forward(entry.chunkId);
    } else {
        if (!m_isLastStage && !m_bwdP2PReceived[entry.chunkId]) {
            NS_LOG_DEBUG("t=" << Simulator::Now().GetNanoSeconds() << "ns, rank=" << m_rank
                              << " waiting for BwdP2P chunk=" << entry.chunkId);
            return;
        }
        m_scheduleIdx++;
        Backward(entry.chunkId);
    }
}

} // namespace ns3