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
    m_dpRdmaCC->SetRank(m_rankInGroup); // rank within DP group
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

    // Install RdmaCC on this node
    m_dpRdmaCC -> SetStartTime(Seconds(1e9));          // Not to start early.
    uint32_t app_index = GetNode()->AddApplication(m_dpRdmaCC);
    GetNode()->GetObject<RdmaDriver>()->m_rdma->m_agent_app = app_index;

    // Initialize P2P tracking arrays
    m_fwdP2PReceived.resize(m_numChunks, false);
    m_bwdP2PReceived.resize(m_numChunks, false);

    NS_LOG_INFO("HybridCC rank=" << m_rank
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
    NS_LOG_INFO("HybridCC StartApplication rank=" << m_rank
                                                  << " phase=" << m_phase << " scheduleMode=" << (int)m_scheduleMode);

    if (m_scheduleMode == 1) {
        // Static schedule mode
        BuildSchedule();
        ExecuteNextEntry();
    } else {
        // Event-driven mode
        if (m_isFirstStage) {
            m_fwdReadyCount = m_numChunks;
        }
        m_bwdReadyCount = 0;
        m_fwdChunkIdx = 0;
        m_bwdChunkIdx = 0;
        m_inflightFwd = 0;
        ScheduleNext_EventDriven();
    }
}

void HybridCC::StopApplication(void) {
    NS_LOG_INFO("HybridCC StopApplication rank=" << m_rank);
}

void HybridCC::DoDispose(void) {
    Application::DoDispose();
}

// ===== Forward operation: AG → tf → [P2P] =====

void HybridCC::Forward(uint16_t chunkId) {
    NS_LOG_INFO("HybridCC rank=" << m_rank << " Forward chunk=" << chunkId);
    m_currentChunkId = chunkId;
    m_phase = FWD_AG;

    // Reset RdmaCC and configure for Allgather
    m_dpRdmaCC->Reset();
    uint64_t agSize = m_chunkSizeFwd / m_dpGroupSize;
    m_dpRdmaCC->SetChunk(agSize, 1);
    m_dpRdmaCC->SetCompletionCallback(
        MakeCallback(&HybridCC::OnFwdAGComplete, this));
    m_dpRdmaCC->Allgather();
    m_dpRdmaCC->StartApplication();
}

void HybridCC::OnFwdAGComplete() {
    NS_LOG_INFO("HybridCC rank=" << m_rank << " FwdAG complete chunk=" << m_currentChunkId);
    m_phase = FWD_COMPUTE;
    Simulator::Schedule(m_tf, &HybridCC::OnFwdComputeDone, this);
}

void HybridCC::OnFwdComputeDone() {
    if (m_isLastStage) {
        // Last stage: no P2P forward, self-trigger backward if train mode
        if (m_train) {
            if (m_scheduleMode == 0) {
                m_bwdReadyCount++;
            } else {
                m_bwdP2PReceived[m_currentChunkId] = true;
            }
        }
        // Schedule next operation
        ScheduleNext();
    } else {
        // P2P forward to next stage
        m_phase = FWD_P2P;
        SendP2P(GetP2PFwdPartner(), m_chunkSizeFwd);
    }
}

// ===== Backward operation: AG → tb → RS → tb → [P2P] =====

void HybridCC::Backward(uint16_t chunkId) {
    NS_LOG_INFO("HybridCC rank=" << m_rank << " Backward chunk=" << chunkId);
    m_currentChunkId = chunkId;
    m_phase = BWD_AG;

    // Reset RdmaCC and configure for Allgather
    m_dpRdmaCC->Reset();
    uint64_t agSize = m_chunkSizeBwd / m_dpGroupSize;
    m_dpRdmaCC->SetChunk(agSize, 1);
    m_dpRdmaCC->SetCompletionCallback(
        MakeCallback(&HybridCC::OnBwdAGComplete, this));
    m_dpRdmaCC->Allgather();
    m_dpRdmaCC->StartApplication();
}

void HybridCC::OnBwdAGComplete() {
    NS_LOG_INFO("HybridCC rank=" << m_rank << " BwdAG complete chunk=" << m_currentChunkId);
    m_phase = BWD_COMPUTE_1;
    Simulator::Schedule(m_tb, &HybridCC::OnBwdCompute1Done, this);
}

void HybridCC::OnBwdCompute1Done() {
    m_phase = BWD_RS;

    // Reset RdmaCC and configure for ReduceScatter
    m_dpRdmaCC->Reset();
    uint64_t rsSize = m_chunkSizeBwd / m_dpGroupSize;
    m_dpRdmaCC->SetChunk(rsSize, 1);
    m_dpRdmaCC->SetCompletionCallback(
        MakeCallback(&HybridCC::OnBwdRSComplete, this));
    m_dpRdmaCC->ReduceScatter();
    m_dpRdmaCC->StartApplication();
}

void HybridCC::OnBwdRSComplete() {
    NS_LOG_INFO("HybridCC rank=" << m_rank << " BwdRS complete chunk=" << m_currentChunkId);
    m_phase = BWD_COMPUTE_2;
    Simulator::Schedule(m_tb, &HybridCC::OnBwdCompute2Done, this);
}

void HybridCC::OnBwdCompute2Done() {
    if (m_isFirstStage) {
        // First stage: no P2P backward
        ScheduleNext();
    } else {
        // P2P backward to previous stage
        m_phase = BWD_P2P;
        SendP2P(GetP2PBwdPartner(), m_chunkSizeBwd);
    }
}

// ===== P2P communication =====

void HybridCC::SendP2P(uint16_t destRank, uint64_t size) {
    NS_LOG_INFO("HybridCC rank=" << m_rank << " SendP2P to rank=" << destRank
                                 << " size=" << size << " chunk=" << m_currentChunkId);
    Ptr<RdmaDriver> rdma = GetNode()->GetObject<RdmaDriver>();
    uint16_t sport = GetNextPort();
    rdma->AddQueuePair(size, m_pg, m_ip, m_nodes[destRank].ip,
                       sport, m_nodes[destRank].port,
                       m_win, m_baseRtt,
                       MakeCallback(&HybridCC::OnP2PSendComplete, this));
}

void HybridCC::OnP2PSendComplete() {
    if (m_phase == FWD_P2P) {
        // Notify next stage that forward P2P arrived
        Ptr<HybridCC> dst = m_ip2hybrid(m_nodes[GetP2PFwdPartner()].ip);
        dst->OnP2PFwdRecv(m_currentChunkId);
    } else if (m_phase == BWD_P2P) {
        // Notify previous stage that backward P2P arrived
        Ptr<HybridCC> dst = m_ip2hybrid(m_nodes[GetP2PBwdPartner()].ip);
        dst->OnP2PBwdRecv(m_currentChunkId);
    }
    ScheduleNext();
}

// ===== P2P receive callbacks =====

void HybridCC::OnP2PFwdRecv(uint16_t chunkId) {
    NS_LOG_INFO("HybridCC rank=" << m_rank << " OnP2PFwdRecv chunk=" << chunkId);
    if (m_scheduleMode == 0) {
        m_fwdReadyCount++;
        ScheduleNext_EventDriven();
    } else {
        m_fwdP2PReceived[chunkId] = true;
        ExecuteNextEntry();
    }
}

void HybridCC::OnP2PBwdRecv(uint16_t chunkId) {
    NS_LOG_INFO("HybridCC rank=" << m_rank << " OnP2PBwdRecv chunk=" << chunkId);
    if (m_scheduleMode == 0) {
        m_bwdReadyCount++;
        ScheduleNext_EventDriven();
    } else {
        m_bwdP2PReceived[chunkId] = true;
        ExecuteNextEntry();
    }
}

// ===== Scheduling =====

void HybridCC::ScheduleNext() {
    if (m_scheduleMode == 0) {
        ScheduleNext_EventDriven();
    } else {
        ExecuteNextEntry();
    }
}

// ----- Event-driven scheduling -----

void HybridCC::ScheduleNext_EventDriven() {
    // Check if all done
    bool allFwdDone = (m_fwdChunkIdx >= m_numChunks);
    bool allBwdDone = (!m_train) || (m_bwdChunkIdx >= m_numChunks);

    if (allFwdDone && allBwdDone) {
        m_phase = DONE;
        NS_LOG_INFO("HybridCC rank=" << m_rank << " ALL DONE");
        return;
    }

    // Priority: backward > forward (to free memory)
    if (m_train && m_bwdReadyCount > 0 && m_bwdChunkIdx < m_numChunks) {
        m_bwdReadyCount--;
        Backward(m_bwdChunkIdx++);
        return;
    }

    // Forward: check ready count and inflight limit
    if (m_fwdReadyCount > 0 && m_fwdChunkIdx < m_numChunks
        && m_inflightFwd < m_maxInflight) {
        m_fwdReadyCount--;
        m_inflightFwd++;
        Forward(m_fwdChunkIdx++);
        return;
    }

    // Idle: waiting for callbacks
    NS_LOG_INFO("HybridCC rank=" << m_rank << " idle "
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

    NS_LOG_INFO("HybridCC rank=" << m_rank
                                 << " BuildSchedule: warmup=" << num_warmup
                                 << " steady_1f1b=" << num_1f1b
                                 << " cooldown=" << num_warmup);

    // Phase 1: Warmup (forward only)
    for (uint16_t i = 0; i < num_warmup; i++) {
        ScheduleEntry e;
        e.type = OP_FORWARD;
        e.chunkId = i;
        m_schedule.push_back(e);
    }

    // Phase 2: Steady state 1F1B
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

    // Phase 3: Cooldown (backward only)
    for (uint16_t i = 0; i < num_warmup; i++) {
        if (m_train) {
            ScheduleEntry eb;
            eb.type = OP_BACKWARD;
            eb.chunkId = static_cast<uint16_t>(num_1f1b + i);
            m_schedule.push_back(eb);
        }
    }

    m_scheduleIdx = 0;

    // Log schedule
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
        NS_LOG_INFO("HybridCC rank=" << m_rank << " ALL DONE (static)");
        return;
    }

    ScheduleEntry &entry = m_schedule[m_scheduleIdx];

    if (entry.type == OP_FORWARD) {
        // Check P2P dependency: non-first stages need fwd P2P to arrive first
        // (except chunk 0 for first stage which starts immediately)
        if (!m_isFirstStage && !m_fwdP2PReceived[entry.chunkId]) {
            NS_LOG_INFO("HybridCC rank=" << m_rank
                                         << " waiting for FwdP2P chunk=" << entry.chunkId);
            return; // Will be retried when OnP2PFwdRecv fires
        }
        m_scheduleIdx++;
        Forward(entry.chunkId);
    } else {
        // Backward: check P2P dependency
        if (!m_isLastStage && !m_bwdP2PReceived[entry.chunkId]) {
            NS_LOG_INFO("HybridCC rank=" << m_rank
                                         << " waiting for BwdP2P chunk=" << entry.chunkId);
            return; // Will be retried when OnP2PBwdRecv fires
        }
        m_scheduleIdx++;
        Backward(entry.chunkId);
    }
}

} // namespace ns3