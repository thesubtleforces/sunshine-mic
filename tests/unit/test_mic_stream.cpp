/**
 * @file tests/unit/test_mic_stream.cpp
 * @brief Test src/mic_stream.* sequence handling.
 */

// test includes
#include "../tests_common.h"

// standard includes
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <vector>

// lib includes
#include <boost/asio.hpp>
#include <opus/opus.h>

// local includes
#include <src/config.h>
#include <src/mic_packet.h>
#include <src/mic_stream.h>

using mic::classify_sequence;
using mic::seq_action_e;

TEST(MicSequenceTest, FirstPacketAccepted) {
  const auto d = classify_sequence(std::nullopt, 1234);
  EXPECT_EQ(d.action, seq_action_e::accept);
  EXPECT_EQ(d.plc_frames, 0);
}

TEST(MicSequenceTest, InOrderAccepted) {
  const auto d = classify_sequence(std::uint16_t {41}, 42);
  EXPECT_EQ(d.action, seq_action_e::accept);
  EXPECT_EQ(d.plc_frames, 0);
}

TEST(MicSequenceTest, WrapAroundAccepted) {
  // 65535 -> 0 is the next packet, not a client restart.
  const auto d = classify_sequence(std::uint16_t {65535}, 0);
  EXPECT_EQ(d.action, seq_action_e::accept);
  EXPECT_EQ(d.plc_frames, 0);
}

TEST(MicSequenceTest, SmallGapTriggersPlc) {
  const auto two = classify_sequence(std::uint16_t {5}, 8);
  EXPECT_EQ(two.action, seq_action_e::accept);
  EXPECT_EQ(two.plc_frames, 2);

  // The largest gap that is still concealed.
  const auto max = classify_sequence(std::uint16_t {5}, 11);
  EXPECT_EQ(max.action, seq_action_e::accept);
  EXPECT_EQ(max.plc_frames, mic::kMaxPlcFrames);
}

TEST(MicSequenceTest, LargeGapResyncs) {
  const auto d = classify_sequence(std::uint16_t {5}, 12);
  EXPECT_EQ(d.action, seq_action_e::resync);
  EXPECT_EQ(d.plc_frames, 0);
}

TEST(MicSequenceTest, DuplicateDropped) {
  const auto d = classify_sequence(std::uint16_t {5}, 5);
  EXPECT_EQ(d.action, seq_action_e::drop);
  EXPECT_EQ(d.plc_frames, 0);
}

TEST(MicSequenceTest, DuplicateOfZeroDropped) {
  // A repeated packet 0 is a duplicate, not a client restart.
  const auto d = classify_sequence(std::uint16_t {0}, 0);
  EXPECT_EQ(d.action, seq_action_e::drop);
}

TEST(MicSequenceTest, LateDropped) {
  const auto slightly_late = classify_sequence(std::uint16_t {5}, 3);
  EXPECT_EQ(slightly_late.action, seq_action_e::drop);

  // Late across the wrap point: 65530 is 11 packets behind 5.
  const auto late_across_wrap = classify_sequence(std::uint16_t {5}, 65530);
  EXPECT_EQ(late_across_wrap.action, seq_action_e::drop);
}

TEST(MicSequenceTest, ZeroAfterToggleResyncs) {
  // The client restarts its counter at 0 whenever the user toggles the microphone.
  const auto d = classify_sequence(std::uint16_t {1234}, 0);
  EXPECT_EQ(d.action, seq_action_e::resync);
  EXPECT_EQ(d.plc_frames, 0);
}

TEST(MicSequenceTest, FarBehindResyncs) {
  const auto d = classify_sequence(std::uint16_t {100}, 40000);
  EXPECT_EQ(d.action, seq_action_e::resync);
  EXPECT_EQ(d.plc_frames, 0);
}

namespace {
  namespace asio = boost::asio;
  using udp = asio::ip::udp;
  using namespace std::chrono_literals;

  constexpr std::uint16_t kTestBasePort = 38989;  ///< Keeps the tests clear of a running Sunshine; the mic port is base + 12.
  constexpr int kFrameSamples = 960;  ///< One 20 ms Opus frame at 48 kHz.

  /**
   * @brief Audio captured by the fake virtual microphone, shared with the test body.
   */
  struct captured_t {
    std::mutex mutex;  ///< Guards `samples`.
    std::vector<std::int16_t> samples;  ///< Everything written to the fake microphone.
    std::atomic<int> created {0};  ///< Number of fake microphones created.
    std::atomic<int> destroyed {0};  ///< Number of fake microphones destroyed.

    std::size_t size() {
      std::lock_guard lock {mutex};
      return samples.size();
    }

    int peak() {
      std::lock_guard lock {mutex};
      int peak = 0;
      for (const auto sample : samples) {
        peak = std::max(peak, std::abs(static_cast<int>(sample)));
      }
      return peak;
    }
  };

  /**
   * @brief Virtual microphone that records what it is given.
   */
  class fake_sink_t: public platf::mic_sink_t {
  public:
    explicit fake_sink_t(std::shared_ptr<captured_t> captured):
        captured_ {std::move(captured)} {
      ++captured_->created;
    }

    ~fake_sink_t() override {
      ++captured_->destroyed;
    }

    int write(const std::int16_t *pcm, std::size_t frames) override {
      std::lock_guard lock {captured_->mutex};
      captured_->samples.insert(captured_->samples.end(), pcm, pcm + frames);
      return 0;
    }

  private:
    std::shared_ptr<captured_t> captured_;
  };

  /**
   * @brief Sends real Opus datagrams over loopback to the microphone socket of mic::register_session().
   */
  class MicStreamTest: public ::testing::Test {
  protected:
    void SetUp() override {
      saved_port_ = config::sunshine.port;
      config::sunshine.port = kTestBasePort;

      captured_ = std::make_shared<captured_t>();
      mic::set_sink_factory_for_testing([captured = captured_](const platf::mic_sink_config_t &) {
        return std::unique_ptr<platf::mic_sink_t> {new fake_sink_t {captured}};
      });

      int err = 0;
      encoder_ = opus_encoder_create(48000, 1, OPUS_APPLICATION_VOIP, &err);
      ASSERT_EQ(err, OPUS_OK);
    }

    void TearDown() override {
      mic::unregister_session(1);
      mic::unregister_session(2);
      mic::set_sink_factory_for_testing(nullptr);
      config::sunshine.port = saved_port_;
      if (encoder_) {
        opus_encoder_destroy(encoder_);
      }
    }

    static std::uint16_t mic_port() {
      return static_cast<std::uint16_t>(kTestBasePort + 12);
    }

    static asio::ip::address loopback() {
      return asio::ip::make_address("127.0.0.1");
    }

    /**
     * @brief Encode the next 20 ms of a 440 Hz tone.
     */
    std::vector<std::uint8_t> encode_frame() {
      std::array<opus_int16, kFrameSamples> pcm {};
      for (auto &sample : pcm) {
        sample = static_cast<opus_int16>(8000.0 * std::sin(2.0 * M_PI * 440.0 * static_cast<double>(position_++) / 48000.0));
      }
      std::vector<std::uint8_t> out(1276);
      const auto n = opus_encode(encoder_, pcm.data(), kFrameSamples, out.data(), static_cast<opus_int32>(out.size()));
      EXPECT_GT(n, 0);
      out.resize(n > 0 ? static_cast<std::size_t>(n) : 0);
      return out;
    }

    /**
     * @brief Send one legacy-header datagram from loopback; each call uses a new source port, as a client does after a mic toggle.
     */
    void send_packet(std::uint16_t seq, const std::vector<std::uint8_t> &payload) {
      std::vector<std::uint8_t> datagram {0x00, mic_packet::kTypeOpus, static_cast<std::uint8_t>(seq), static_cast<std::uint8_t>(seq >> 8)};
      for (int i = 0; i < 4; ++i) {
        datagram.push_back(static_cast<std::uint8_t>(1000u >> (8 * i)));  // timestamp
      }
      for (int i = 0; i < 4; ++i) {
        datagram.push_back(static_cast<std::uint8_t>(mic_packet::kMagicSsrc >> (8 * i)));
      }
      datagram.insert(datagram.end(), payload.begin(), payload.end());

      asio::io_context io;
      udp::socket sock {io, udp::v4()};
      sock.send_to(asio::buffer(datagram), udp::endpoint {loopback(), mic_port()});
    }

    void send_frame(std::uint16_t seq) {
      send_packet(seq, encode_frame());
    }

    /**
     * @brief Wait until the fake microphone holds at least `count` samples.
     */
    bool wait_for(std::size_t count) {
      const auto deadline = std::chrono::steady_clock::now() + 3s;
      while (captured_->size() < count) {
        if (std::chrono::steady_clock::now() > deadline) {
          return false;
        }
        std::this_thread::sleep_for(5ms);
      }
      return true;
    }

    std::shared_ptr<captured_t> captured_;  ///< What the fake microphone received.
    OpusEncoder *encoder_ = nullptr;  ///< Encoder producing the test datagrams.
    std::uint64_t position_ = 0;  ///< Tone phase in samples.
    std::uint16_t saved_port_ = 0;  ///< Base port to restore after the test.
  };
}  // namespace

TEST_F(MicStreamTest, DecodesDatagramsFromRegisteredPeer) {
  mic::register_session(1, loopback());
  for (std::uint16_t seq = 0; seq < 5; ++seq) {
    send_frame(seq);
  }

  ASSERT_TRUE(wait_for(5 * kFrameSamples));
  EXPECT_EQ(captured_->size(), 5u * kFrameSamples);
  EXPECT_GT(captured_->peak(), 1000);  // the decoded tone, not silence
  EXPECT_EQ(captured_->created, 1);
}

TEST_F(MicStreamTest, IgnoresDatagramsFromOtherAddresses) {
  mic::register_session(1, asio::ip::make_address("192.0.2.10"));
  for (std::uint16_t seq = 0; seq < 3; ++seq) {
    send_frame(seq);
  }

  std::this_thread::sleep_for(300ms);
  EXPECT_EQ(captured_->size(), 0u);
}

TEST_F(MicStreamTest, ConcealsSmallSequenceGaps) {
  mic::register_session(1, loopback());
  send_frame(0);
  send_frame(1);
  send_frame(2);
  static_cast<void>(encode_frame());  // frames 3 and 4 are lost on the network
  static_cast<void>(encode_frame());
  send_frame(5);

  ASSERT_TRUE(wait_for(6 * kFrameSamples));  // 4 decoded + 2 concealed
  std::this_thread::sleep_for(100ms);
  EXPECT_EQ(captured_->size(), 6u * kFrameSamples);
}

TEST_F(MicStreamTest, ResyncsWhenTheClientRestartsItsCounter) {
  mic::register_session(1, loopback());
  for (std::uint16_t seq = 0; seq < 10; ++seq) {
    send_frame(seq);
  }
  for (std::uint16_t seq = 0; seq < 5; ++seq) {
    send_frame(seq);  // the user toggled the microphone: sequence restarts at 0
  }

  ASSERT_TRUE(wait_for(15 * kFrameSamples));
  std::this_thread::sleep_for(100ms);
  EXPECT_EQ(captured_->size(), 15u * kFrameSamples);
}

TEST_F(MicStreamTest, DropsDuplicateAndLatePackets) {
  mic::register_session(1, loopback());
  const auto f0 = encode_frame();
  const auto f1 = encode_frame();
  const auto f2 = encode_frame();
  const auto f3 = encode_frame();
  send_packet(0, f0);
  send_packet(1, f1);
  send_packet(2, f2);
  send_packet(2, f2);  // duplicate
  send_packet(1, f1);  // late
  send_packet(3, f3);

  ASSERT_TRUE(wait_for(4 * kFrameSamples));
  std::this_thread::sleep_for(100ms);
  EXPECT_EQ(captured_->size(), 4u * kFrameSamples);
}

TEST_F(MicStreamTest, UnregisterReleasesSocketAndSink) {
  mic::register_session(1, loopback());
  EXPECT_EQ(captured_->created, 1);

  mic::unregister_session(1);
  EXPECT_EQ(captured_->destroyed, 1);

  // The microphone port is free again.
  asio::io_context io;
  udp::socket probe {io};
  boost::system::error_code ec;
  probe.open(udp::v4(), ec);
  probe.bind(udp::endpoint {udp::v4(), mic_port()}, ec);
  EXPECT_FALSE(ec) << ec.message();
}

TEST_F(MicStreamTest, NewestSessionOwnsTheMicrophoneAndTheOlderOneResumes) {
  mic::register_session(1, loopback());
  mic::register_session(2, loopback());
  EXPECT_EQ(captured_->created, 1);  // both sessions share one virtual microphone

  mic::unregister_session(2);
  EXPECT_EQ(captured_->destroyed, 0);  // session 1 still holds it

  send_frame(0);
  ASSERT_TRUE(wait_for(kFrameSamples));

  mic::unregister_session(1);
  EXPECT_EQ(captured_->destroyed, 1);
}

TEST_F(MicStreamTest, SessionWithoutVirtualMicrophoneDropsAudioQuietly) {
  mic::set_sink_factory_for_testing([](const platf::mic_sink_config_t &) {
    return std::unique_ptr<platf::mic_sink_t> {};
  });

  mic::register_session(1, loopback());
  send_frame(0);
  std::this_thread::sleep_for(100ms);
  mic::unregister_session(1);  // must neither crash nor hang

  EXPECT_EQ(captured_->created, 0);
}

TEST_F(MicStreamTest, BindFailureSkipsTheVirtualMicrophoneAndTheNextRegistrationRetries) {
  // Something else already holds the microphone port.
  asio::io_context io;
  udp::socket blocker {io};
  boost::system::error_code ec;
  blocker.open(udp::v4(), ec);
  blocker.bind(udp::endpoint {udp::v4(), mic_port()}, ec);
  ASSERT_FALSE(ec) << ec.message();

  // No audio can ever arrive, so no silent device may become the system default source.
  mic::register_session(1, loopback());
  EXPECT_EQ(captured_->created, 0);

  mic::unregister_session(1);  // clean even though nothing was bound
  EXPECT_EQ(captured_->destroyed, 0);

  // Once the port is free the next registration sets everything up.
  blocker.close(ec);
  mic::register_session(1, loopback());
  EXPECT_EQ(captured_->created, 1);

  send_frame(0);
  ASSERT_TRUE(wait_for(kFrameSamples));
}

TEST_F(MicStreamTest, UnregisterIsIdempotent) {
  mic::unregister_session(42);  // never registered
  mic::register_session(1, loopback());
  mic::unregister_session(1);
  mic::unregister_session(1);
  EXPECT_EQ(captured_->destroyed, 1);
}
