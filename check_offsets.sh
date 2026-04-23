#!/bin/bash
# check_offsets.sh: Auto-detect BPF hook offsets from compiled ns3 libraries
# Run this after recompilation to check if offsets changed.
#
# Usage: bash check_offsets.sh [path_to_build_dir]

BUILD="${1:-/root/Vedrfolnir/Vedrfolnir/build}"
APPS_LIB="$BUILD/libns3.18-applications-debug.so"
CORE_LIB="$BUILD/libns3.18-core-debug.so"

echo "=== BPF Hook Offset Checker ==="
echo "Apps lib: $APPS_LIB"
echo "Core lib: $CORE_LIB"
echo ""

# --- m_rank offset from SetRank ---
echo "--- m_rank offset (from RdmaCC::SetRank) ---"
SETRANK_LINE=$(nm "$APPS_LIB" | grep 'T _ZN3ns36RdmaCC7SetRankEt')
SETRANK_ADDR=$(echo "$SETRANK_LINE" | awk '{print $1}')
if [ -z "$SETRANK_ADDR" ]; then
    echo "ERROR: SetRank not found!"
else
    echo "SetRank symbol: $SETRANK_LINE"
    # Strip leading zeros for objdump matching
    SETRANK_SHORT=$(echo "$SETRANK_ADDR" | sed 's/^0*//')
    RANK_HEX=$(objdump -d "$APPS_LIB" | sed -n "/${SETRANK_SHORT}:/,/retq/p" | grep "mov.*%dx,0x" | head -1 | grep -oP '0x\K[0-9a-f]+(?=\(%rax\))')
    if [ -n "$RANK_HEX" ]; then
        RANK_DEC=$(printf "%d" "0x$RANK_HEX")
        echo "m_rank offset: 0x$RANK_HEX ($RANK_DEC)"
    else
        echo "Raw disassembly:"
        objdump -d "$APPS_LIB" | sed -n "/${SETRANK_SHORT}:/,/retq/p" | head -20
    fi
    echo "  -> If changed, update M_RANK_OFFSET and RANK_OFFSET in ns3-hook.py"
fi
echo ""

# --- m_currentTs offset from DefaultSimulatorImpl::Now() ---
echo "--- m_currentTs offset (from DefaultSimulatorImpl::Now()) ---"
NOW_LINE=$(nm "$CORE_LIB" | grep 'T _ZNK3ns320DefaultSimulatorImpl3NowEv')
NOW_ADDR=$(echo "$NOW_LINE" | awk '{print $1}')
if [ -z "$NOW_ADDR" ]; then
    echo "ERROR: DefaultSimulatorImpl::Now not found!"
else
    echo "Now() symbol: $NOW_LINE"
    NOW_SHORT=$(echo "$NOW_ADDR" | sed 's/^0*//')
    TS_HEX=$(objdump -d "$CORE_LIB" | sed -n "/${NOW_SHORT}:/,/retq/p" | grep "mov.*0x.*(%rax),%rdx" | head -1 | grep -oP '0x\K[0-9a-f]+(?=\(%rax\))')
    if [ -n "$TS_HEX" ]; then
        TS_DEC=$(printf "%d" "0x$TS_HEX")
        echo "m_currentTs offset: 0x$TS_HEX ($TS_DEC)"
    else
        echo "Raw disassembly:"
        objdump -d "$CORE_LIB" | sed -n "/${NOW_SHORT}:/,/retq/p" | head -20
    fi
    echo "  -> If changed, update M_CURRENT_TS_OFFSET and CURRENT_TS_OFFSET in ns3-hook.py"
fi
echo ""

# --- Symbol addresses ---
echo "--- Symbol addresses (auto-resolved by nm, no manual update needed) ---"
nm "$APPS_LIB" | grep ' T _ZN3ns36RdmaCC4SendEt' | awk '{print "  RdmaCC::Send: 0x"$1}'
nm "$APPS_LIB" | grep ' T _ZN3ns36RdmaCC15SendChunkFinishEv' | awk '{print "  SendChunkFinish: 0x"$1}'
nm "$CORE_LIB" | grep ' b _ZZN3ns3L8PeekImplEvE4impl' | awk '{print "  PeekImpl()::impl: 0x"$1}'
echo ""

# --- Current values in ns3-hook.py ---
echo "--- Current values in ns3-hook.py ---"
grep -n "M_RANK_OFFSET\|M_CURRENT_TS_OFFSET\|CURRENT_TS_OFFSET\|RANK_OFFSET" /root/Vedrfolnir/ns3-hook.py | grep -v "^#" | head -10

echo ""
echo "=== Done. If offsets changed, update the values in ns3-hook.py ==="
echo "Python section: M_RANK_OFFSET, M_CURRENT_TS_OFFSET"
echo "BPF_PROGRAM section: CURRENT_TS_OFFSET, RANK_OFFSET"