/**
 * @file src/mic_stream.cpp
 * @brief Client-to-host microphone stream: sequencing helpers and session registry.
 */

// local includes
#include "mic_stream.h"

namespace mic {

  namespace {
    constexpr int kRestartDistance = 3000;  ///< A packet this far behind the last one is a client restart, not a late packet.
  }  // namespace

  seq_decision_t classify_sequence(std::optional<std::uint16_t> last_seq, std::uint16_t seq) {
    if (!last_seq) {
      return {seq_action_e::accept, 0};
    }

    // Signed distance in sequence space, so the 16-bit wrap needs no special case.
    const auto distance = static_cast<std::int16_t>(static_cast<std::uint16_t>(seq - *last_seq));

    if (distance == 0) {
      return {seq_action_e::drop, 0};  // exact duplicate
    }

    if (distance > 0) {
      if (distance <= 1 + kMaxPlcFrames) {
        return {seq_action_e::accept, distance - 1};
      }
      return {seq_action_e::resync, 0};
    }

    // Behind the last accepted packet: either the client restarted its counter
    // (it does so whenever the user toggles the microphone) or the packet is late.
    if (seq == 0 || distance < -kRestartDistance) {
      return {seq_action_e::resync, 0};
    }
    return {seq_action_e::drop, 0};
  }

  void register_session(std::uint32_t launch_session_id, const boost::asio::ip::address &peer) {
    (void) launch_session_id;
    (void) peer;
  }

  void unregister_session(std::uint32_t launch_session_id) {
    (void) launch_session_id;
  }

}  // namespace mic
