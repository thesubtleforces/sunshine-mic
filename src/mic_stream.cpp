/**
 * @file src/mic_stream.cpp
 * @brief Client-to-host microphone stream: sequencing helpers and session registry.
 */

// standard includes
#include <array>
#include <chrono>
#include <map>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

// lib includes
#include <boost/asio.hpp>
#include <opus/opus.h>

// local includes
#include "config.h"
#include "logging.h"
#include "mic_packet.h"
#include "mic_stream.h"
#include "network.h"
#include "platform/common.h"
#include "rtsp.h"
#include "stream.h"
#include "utility.h"

using namespace std::literals;

namespace mic {

  namespace {
    namespace asio = boost::asio;
    using udp = asio::ip::udp;

    constexpr int kRestartDistance = 3000;  ///< A packet this far behind the last one is a client restart, not a late packet.
    constexpr auto kStatsInterval = 10s;  ///< How often the receive counters are logged at debug level.
    constexpr auto kForeignLogInterval = 30s;  ///< How often datagrams from an unexpected address are logged.

    /**
     * @brief Deleter for Opus decoders.
     */
    struct opus_decoder_deleter_t {
      void operator()(OpusDecoder *decoder) const {
        opus_decoder_destroy(decoder);
      }
    };

    /**
     * @brief Receive state of one registered stream session.
     */
    struct entry_t {
      std::uint64_t order;  ///< Registration counter; the entry with the highest value owns the microphone.
      asio::ip::address peer;  ///< Normalised client address; microphone datagrams must come from it.
      std::unique_ptr<OpusDecoder, opus_decoder_deleter_t> decoder;  ///< 48 kHz mono decoder for this client's audio.
      std::optional<std::uint16_t> last_seq;  ///< Last accepted sequence number.
      int last_frame_samples = 960;  ///< Size of the last decoded frame; reused to conceal lost frames.
      std::uint64_t decoded = 0;  ///< Frames decoded.
      std::uint64_t concealed = 0;  ///< Frames synthesised to cover lost packets.
      std::uint64_t dropped = 0;  ///< Duplicate or late packets discarded.
      std::uint64_t invalid = 0;  ///< Datagrams that were not valid microphone packets.
      std::uint64_t decode_errors = 0;  ///< Packets Opus refused to decode.
    };

    /**
     * @brief Process-wide microphone receive state.
     */
    struct state_t {
      std::mutex lifecycle_mutex;  ///< Serialises register/unregister (socket, thread and sink lifetime).
      std::mutex mutex;  ///< Guards `sessions`, `sink`, `sink_factory` and the log timestamps.
      std::map<std::uint32_t, entry_t> sessions;  ///< Registered sessions by launch-session ID.
      std::uint64_t next_order = 0;  ///< Next registration counter value.
      std::shared_ptr<platf::mic_sink_t> sink;  ///< Virtual microphone shared by all sessions; null when unavailable.
      sink_factory_t sink_factory;  ///< Test override for the virtual microphone factory.
      asio::io_context io;  ///< Runs the receive loop.
      udp::socket sock {io};  ///< Microphone socket bound to base port + 12 while sessions exist.
      udp::endpoint from;  ///< Source of the datagram being received.
      std::array<char, 2048> buf {};  ///< Receive buffer; larger than any client datagram.
      std::thread thread;  ///< The `mic::recv` thread; running exactly while `sock` is open.
      std::chrono::steady_clock::time_point last_stats;  ///< When the counters were last logged.
      std::chrono::steady_clock::time_point last_foreign_log;  ///< When an unexpected source was last logged.
    };

    /**
     * @brief Get the process-wide state.
     *
     * Intentionally leaked: the receive thread and socket must not be torn down by static destruction order.
     */
    state_t &state() {
      static auto *instance = new state_t;
      return *instance;
    }

    /**
     * @brief Find the session that currently owns the microphone (the most recent registration).
     */
    entry_t *find_owner(state_t &s) {
      entry_t *owner = nullptr;
      for (auto &[id, entry] : s.sessions) {
        if (!owner || entry.order > owner->order) {
          owner = &entry;
        }
      }
      return owner;
    }

    void on_receive(const boost::system::error_code &ec, std::size_t bytes);

    /**
     * @brief Queue the next asynchronous receive on the microphone socket.
     */
    void arm_receive(state_t &s) {
      s.sock.async_receive_from(asio::buffer(s.buf), s.from, &on_receive);
    }

    /**
     * @brief Handle one datagram: route it to the owning session, decode it and feed the virtual microphone.
     */
    void on_receive(const boost::system::error_code &ec, std::size_t bytes) {
      auto &s = state();

      // The socket was closed because the last session ended.
      if (ec == asio::error::operation_aborted || ec == asio::error::bad_descriptor || !s.sock.is_open()) {
        return;
      }

      // Keep receiving whatever happens below.
      auto rearm = util::fail_guard([&s]() {
        arm_receive(s);
      });

      if (ec) {
        if (ec != asio::error::connection_refused && ec != asio::error::connection_reset) {
          BOOST_LOG(error) << "mic: couldn't receive a microphone datagram: "sv << ec.message();
        }
        return;
      }

      const auto packet = mic_packet::parse(reinterpret_cast<const std::uint8_t *>(s.buf.data()), bytes);

      // Decoded audio of this datagram; written to the virtual microphone after the lock is released.
      thread_local std::vector<std::int16_t> pcm;
      pcm.clear();
      std::shared_ptr<platf::mic_sink_t> sink;

      {
        std::lock_guard lock {s.mutex};

        auto *owner = find_owner(s);
        if (!owner) {
          return;
        }

        // Route by IP only: a client's source port changes whenever its microphone is toggled.
        if (net::normalize_address(s.from.address()) != owner->peer) {
          const auto now = std::chrono::steady_clock::now();
          if (now - s.last_foreign_log > kForeignLogInterval) {
            s.last_foreign_log = now;
            BOOST_LOG(debug) << "mic: dropping datagrams from "sv << s.from.address().to_string() << ", which is not the owning session's address"sv;
          }
          return;
        }

        if (!packet) {
          ++owner->invalid;
          return;
        }

        const auto decision = classify_sequence(owner->last_seq, packet->sequence);
        if (decision.action == seq_action_e::drop) {
          ++owner->dropped;
          return;
        }
        if (decision.action == seq_action_e::resync) {
          opus_decoder_ctl(owner->decoder.get(), OPUS_RESET_STATE);
        }

        // Accept any valid frame length: the client's encoder settings are not under our control.
        const int frame_samples = opus_packet_get_nb_samples(packet->payload.data(), static_cast<opus_int32>(packet->payload.size()), kSampleRate);
        if (frame_samples <= 0 || frame_samples > kMaxFrameSamples) {
          ++owner->invalid;
          return;
        }

        pcm.resize(static_cast<std::size_t>(decision.plc_frames + 1) * kMaxFrameSamples);
        std::size_t offset = 0;

        for (int i = 0; i < decision.plc_frames; ++i) {
          const int n = opus_decode(owner->decoder.get(), nullptr, 0, pcm.data() + offset, owner->last_frame_samples, 0);
          if (n > 0) {
            offset += static_cast<std::size_t>(n);
            ++owner->concealed;
          }
        }

        const int n = opus_decode(owner->decoder.get(), reinterpret_cast<const unsigned char *>(packet->payload.data()), static_cast<opus_int32>(packet->payload.size()), pcm.data() + offset, kMaxFrameSamples, 0);
        if (n < 0) {
          ++owner->decode_errors;
          opus_decoder_ctl(owner->decoder.get(), OPUS_RESET_STATE);
          BOOST_LOG(debug) << "mic: opus_decode failed: "sv << opus_strerror(n);
          pcm.clear();
          return;
        }
        offset += static_cast<std::size_t>(n);
        pcm.resize(offset);

        owner->last_seq = packet->sequence;
        owner->last_frame_samples = n;
        ++owner->decoded;
        sink = s.sink;

        const auto now = std::chrono::steady_clock::now();
        if (now - s.last_stats > kStatsInterval) {
          s.last_stats = now;
          BOOST_LOG(debug) << "mic: decoded "sv << owner->decoded << ", concealed "sv << owner->concealed << ", dropped "sv << owner->dropped
                           << ", invalid "sv << owner->invalid << ", decode errors "sv << owner->decode_errors;
        }
      }

      if (sink && !pcm.empty()) {
        sink->write(pcm.data(), pcm.size());
      }
    }

    /**
     * @brief Open and bind the microphone socket at base port + 12.
     * @return false when the socket could not be set up (the stream continues without a microphone).
     */
    bool open_socket(state_t &s) {
      const auto address_family = net::get_effective_address_family(net::af_from_enum_string(config::sunshine.address_family));
      const auto protocol = address_family == net::IPV4 ? udp::v4() : udp::v6();
      const auto port = net::map_port(stream::MIC_STREAM_PORT);

      boost::system::error_code ec;
      s.sock.open(protocol, ec);
      if (ec) {
        BOOST_LOG(error) << "mic: couldn't open the microphone socket: "sv << ec.message();
        return false;
      }

      const auto bind_addr_str = net::get_bind_address(address_family);
      const auto bind_addr = asio::ip::make_address(bind_addr_str, ec);
      if (ec) {
        BOOST_LOG(error) << "mic: invalid bind address "sv << bind_addr_str << ": "sv << ec.message();
        s.sock.close(ec);
        return false;
      }

      s.sock.bind(udp::endpoint(bind_addr, port), ec);
      if (ec) {
        BOOST_LOG(error) << "mic: couldn't bind the microphone socket to port ["sv << port << "]: "sv << ec.message();
        s.sock.close(ec);
        return false;
      }

      return true;
    }
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
    auto &s = state();
    std::lock_guard lifecycle {s.lifecycle_mutex};

    int err = 0;
    std::unique_ptr<OpusDecoder, opus_decoder_deleter_t> decoder {opus_decoder_create(kSampleRate, kChannels, &err)};
    if (err != OPUS_OK || !decoder) {
      BOOST_LOG(error) << "mic: opus_decoder_create failed: "sv << opus_strerror(err) << "; session "sv << launch_session_id << " has no microphone"sv;
      return;
    }

    sink_factory_t factory;
    bool need_sink;
    {
      std::lock_guard lock {s.mutex};

      if (const auto *previous = find_owner(s)) {
        BOOST_LOG(info) << "mic: session "sv << launch_session_id << " takes over the microphone (order "sv << previous->order << ")"sv;
      }

      s.sessions.insert_or_assign(launch_session_id, entry_t {s.next_order++, net::normalize_address(peer), std::move(decoder)});
      BOOST_LOG(info) << "mic: session "sv << launch_session_id << " registered for "sv << peer.to_string();

      need_sink = !s.sink;
      factory = s.sink_factory;
    }

    // Bind the port before creating anything the user can see: without a socket no audio can ever arrive,
    // and a virtual microphone would become the (silent) system default source.
    const bool socket_ready = s.sock.is_open() || open_socket(s);

    // Creating the virtual microphone talks to PipeWire, so it happens outside the data lock.
    if (socket_ready && need_sink) {
      const platf::mic_sink_config_t sink_config {static_cast<std::uint32_t>(kSampleRate), kChannels, config::audio.mic_default_source};
      std::shared_ptr<platf::mic_sink_t> sink = factory ? factory(sink_config) : platf::mic_sink_create(sink_config);
      if (!sink) {
        BOOST_LOG(warning) << "mic: virtual microphone unavailable; client microphone audio will be dropped"sv;
      }

      std::lock_guard lock {s.mutex};
      if (!s.sink) {
        s.sink = std::move(sink);
      }
    }

    if (socket_ready && !s.thread.joinable()) {
      arm_receive(s);
      s.thread = std::thread {[&s]() {
        platf::set_thread_name("mic::recv");
        s.io.run();
      }};
    }
  }

  void unregister_session(std::uint32_t launch_session_id) {
    auto &s = state();
    std::lock_guard lifecycle {s.lifecycle_mutex};

    std::shared_ptr<platf::mic_sink_t> sink_to_destroy;
    std::thread thread_to_join;

    {
      std::lock_guard lock {s.mutex};

      if (s.sessions.erase(launch_session_id) == 0) {
        return;
      }
      BOOST_LOG(info) << "mic: session "sv << launch_session_id << " unregistered"sv;

      if (!s.sessions.empty()) {
        return;  // another session keeps the socket and the virtual microphone
      }

      sink_to_destroy = std::move(s.sink);
      thread_to_join = std::move(s.thread);

      // Never close an asio socket while a receive is pending on another thread: close it on that thread.
      if (s.sock.is_open()) {
        asio::post(s.io, [&s]() {
          boost::system::error_code ec;
          s.sock.close(ec);
        });
      }
    }

    // The pending receive completes as aborted, `io.run()` returns and the join is immediate.
    if (thread_to_join.joinable()) {
      thread_to_join.join();
    }
    s.io.restart();

    // Destroying the virtual microphone restores the system default source.
    sink_to_destroy.reset();
  }

  void set_sink_factory_for_testing(sink_factory_t factory) {
    auto &s = state();
    std::lock_guard lock {s.mutex};
    s.sink_factory = std::move(factory);
  }

}  // namespace mic
