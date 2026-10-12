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
#include <optional>

// lib includes
#include <boost/asio/ip/address.hpp>

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
   * @brief Result of classify_sequence().
   */
  struct seq_decision_t {
    seq_action_e action;  ///< Disposition of the packet.
    int plc_frames;  ///< Number of lost frames to conceal before decoding (only with `accept`).
  };

  /**
   * @brief Wrap-aware classification of a sequence number against the last accepted one.
   * @param last_seq Last accepted sequence number, or `std::nullopt` before the first packet.
   * @param seq Sequence number of the incoming packet.
   * @return Whether to accept, drop or resync, and how many frames of loss to conceal.
   */
  seq_decision_t classify_sequence(std::optional<std::uint16_t> last_seq, std::uint16_t seq);

  /**
   * @brief Start routing microphone packets from `peer` into the host virtual microphone.
   * @param launch_session_id RTSP launch-session ID that identifies the stream session.
   * @param peer RTSP peer address of the client; microphone datagrams must come from this IP.
   */
  void register_session(std::uint32_t launch_session_id, const boost::asio::ip::address &peer);

  /**
   * @brief Stop routing for a session; the last unregistration closes the socket and the virtual microphone.
   * @param launch_session_id RTSP launch-session ID passed to register_session(). Unknown IDs are ignored.
   */
  void unregister_session(std::uint32_t launch_session_id);

}  // namespace mic
