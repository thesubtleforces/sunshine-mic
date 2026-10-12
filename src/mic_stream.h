/**
 * @file src/mic_stream.h
 * @brief Client-to-host microphone stream: sequencing helpers and session registry.
 *
 * A client that negotiated the microphone stream sends 48 kHz Opus datagrams to
 * base-port + 12. The module owns that UDP socket, decodes the audio and writes
 * it into the platform virtual microphone (platf::mic_sink_t).
 */
#pragma once

// standard includes
#include <cstdint>
#include <functional>
#include <memory>
#include <optional>
#include <string_view>

// lib includes
#include <boost/asio/ip/address.hpp>

// local includes
#include "platform/common.h"

namespace mic {

  constexpr int kSampleRate = 48000;  ///< Decoder output sample rate in Hz.
  constexpr int kChannels = 1;  ///< Decoder output channels (Opus downmixes any client layout).
  constexpr int kMaxFrameSamples = 5760;  ///< Largest Opus frame (120 ms) in samples per channel.
  constexpr int kMaxPlcFrames = 5;  ///< Gaps up to this many frames are concealed; larger gaps resync.

  /**
   * @brief What to do with an incoming packet, given the last accepted sequence number.
   */
  enum class seq_action_e {
    accept,  ///< Decode the packet (after concealing `plc_frames` missing frames).
    drop,  ///< Duplicate or late packet; discard it.
    resync,  ///< Client restarted or jumped far; reset the decoder and decode the packet.
  };

  /**
   * @brief Result of sequence_tracker_t::classify().
   */
  struct seq_decision_t {
    seq_action_e action;  ///< Disposition of the packet.
    int plc_frames;  ///< Number of lost frames to conceal before decoding (only with `accept`).
  };

  /**
   * @brief Tracks the sequence numbers of one client's microphone stream.
   *
   * Decides, per datagram, whether to decode it, drop it or restart the decoder, and how many lost frames to
   * conceal first. The client restarts its 16-bit counter at 0 whenever the user toggles the microphone, and the
   * first packet after a toggle may be lost, so a restart is recognised from a short run of consecutive packets
   * that are far behind the last accepted one. Isolated late packets, reordering and duplicates never trigger it.
   */
  class sequence_tracker_t {
  public:
    /**
     * @brief Classify the next datagram. Call exactly once per datagram.
     * @param seq Sequence number from the packet header.
     * @return Whether to accept, drop or resync, and how many frames of loss to conceal.
     */
    seq_decision_t classify(std::uint16_t seq);

    /**
     * @brief Record that a packet was decoded; it becomes the new reference and any restart candidate is cleared.
     * @param seq Sequence number of the decoded packet.
     */
    void accept(std::uint16_t seq);

    /**
     * @brief Forget everything, as for a brand new stream.
     */
    void reset();

    /**
     * @brief Sequence number of the last decoded packet, if any.
     */
    std::optional<std::uint16_t> last() const;

  private:
    std::optional<std::uint16_t> last_;  ///< Last decoded sequence number.
    std::uint16_t run_last_ = 0;  ///< Newest sequence number of the current restart candidate run.
    int run_length_ = 0;  ///< Packets in the current run of increasing, far-behind sequence numbers.
    int run_contiguous_ = 0;  ///< Trailing packets of the run whose numbers followed each other exactly.
  };

  /**
   * @brief Start routing microphone packets from `peer` into the host virtual microphone.
   *
   * Ownership policy (the microphone wire format carries no session or client identity, so routing can only
   * use the source IP address of the datagrams):
   * - The most recently registered session owns the microphone; datagrams are accepted only from its address.
   * - A session from the same client (same non-empty `client_id`) and the same address replaces its earlier
   *   session: that is the client reconnecting or resuming.
   * - Sessions of different, or unknown, clients on the same address cannot be told apart. While two such
   *   sessions are registered no microphone audio from that address is accepted (fail closed); the audio
   *   resumes when one of them ends.
   * - A newer session from another address takes the microphone over; the older one resumes when it ends.
   *
   * @param launch_session_id RTSP launch-session ID that identifies the stream session.
   * @param peer RTSP peer address of the client; microphone datagrams must come from this IP.
   * @param client_id Authenticated identity of the paired client (its certificate); empty when unknown.
   */
  void register_session(std::uint32_t launch_session_id, const boost::asio::ip::address &peer, std::string_view client_id);

  /**
   * @brief Stop routing for a session; the last unregistration closes the socket and the virtual microphone.
   * @param launch_session_id RTSP launch-session ID passed to register_session(). Unknown IDs are ignored.
   */
  void unregister_session(std::uint32_t launch_session_id);

  /**
   * @brief Factory for the host virtual microphone; the default is platf::mic_sink_create().
   */
  using sink_factory_t = std::function<std::unique_ptr<platf::mic_sink_t>(const platf::mic_sink_config_t &)>;

  /**
   * @brief Replace the virtual microphone factory so tests can observe the decoded audio.
   * @param factory The replacement; an empty function restores the platform default.
   */
  void set_sink_factory_for_testing(sink_factory_t factory);

}  // namespace mic
