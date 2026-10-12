/**
 * @file tests/unit/platform/linux/test_mic_source.cpp
 * @brief Test src/platform/linux/mic_source.*
 */

#ifdef __linux__

  // standard includes
  #include <array>
  #include <chrono>
  #include <cmath>
  #include <cstddef>
  #include <cstdint>
  #include <string>
  #include <thread>

  // lib includes
  #include <gtest/gtest.h>

  // local includes
  #include "src/platform/common.h"
  #include "src/platform/linux/mic_source.h"

using platf::pw_mic::ring_t;

namespace {
  /**
   * @brief Push `count` consecutive samples starting at `first`.
   */
  void push_range(ring_t &ring, std::int16_t first, std::size_t count) {
    std::array<std::int16_t, 64> samples {};
    for (std::size_t i = 0; i < count; ++i) {
      samples[i] = static_cast<std::int16_t>(first + static_cast<std::int16_t>(i));
    }
    ring.push(samples.data(), count);
  }
}  // namespace

TEST(MicRingTest, PopFromEmptyReturnsZero) {
  ring_t ring {8};
  std::array<std::int16_t, 4> out {};
  EXPECT_EQ(ring.pop(out.data(), out.size()), 0u);
  EXPECT_EQ(ring.available(), 0u);
}

TEST(MicRingTest, PushThenPopRoundTrips) {
  ring_t ring {8};
  push_range(ring, 1, 3);
  EXPECT_EQ(ring.available(), 3u);

  std::array<std::int16_t, 8> out {};
  ASSERT_EQ(ring.pop(out.data(), out.size()), 3u);
  EXPECT_EQ(out[0], 1);
  EXPECT_EQ(out[1], 2);
  EXPECT_EQ(out[2], 3);
  EXPECT_EQ(ring.available(), 0u);
}

TEST(MicRingTest, OverwriteOldestWhenFull) {
  ring_t ring {8};
  push_range(ring, 1, 12);
  EXPECT_EQ(ring.available(), 8u);

  std::array<std::int16_t, 16> out {};
  ASSERT_EQ(ring.pop(out.data(), out.size()), 8u);
  // Samples 1..4 were overwritten; the newest 8 survive in order.
  for (std::size_t i = 0; i < 8; ++i) {
    EXPECT_EQ(out[i], static_cast<std::int16_t>(5 + i));
  }
}

TEST(MicRingTest, PushLargerThanCapacityKeepsTail) {
  ring_t ring {4};
  std::array<std::int16_t, 10> samples {};
  for (std::size_t i = 0; i < samples.size(); ++i) {
    samples[i] = static_cast<std::int16_t>(1 + i);
  }
  ring.push(samples.data(), samples.size());
  EXPECT_EQ(ring.available(), 4u);

  std::array<std::int16_t, 8> out {};
  ASSERT_EQ(ring.pop(out.data(), out.size()), 4u);
  EXPECT_EQ(out[0], 7);
  EXPECT_EQ(out[3], 10);
}

TEST(MicRingTest, AvailableNeverExceedsCapacity) {
  ring_t ring {6};
  for (int i = 0; i < 5; ++i) {
    push_range(ring, static_cast<std::int16_t>(i * 4), 4);
    EXPECT_LE(ring.available(), ring.capacity());
  }
  EXPECT_EQ(ring.available(), 6u);
}

TEST(MicRingTest, PartialPopAdvancesReader) {
  ring_t ring {8};
  push_range(ring, 1, 6);

  std::array<std::int16_t, 4> first {};
  ASSERT_EQ(ring.pop(first.data(), 2), 2u);
  EXPECT_EQ(first[0], 1);
  EXPECT_EQ(first[1], 2);

  std::array<std::int16_t, 8> rest {};
  ASSERT_EQ(ring.pop(rest.data(), rest.size()), 4u);
  EXPECT_EQ(rest[0], 3);
  EXPECT_EQ(rest[3], 6);
}

TEST(MicRingTest, WrapAroundPreservesOrder) {
  ring_t ring {4};
  push_range(ring, 1, 3);
  std::array<std::int16_t, 2> drained {};
  ASSERT_EQ(ring.pop(drained.data(), drained.size()), 2u);

  // Writing 4,5,6 wraps past the end of the underlying buffer.
  push_range(ring, 4, 3);
  EXPECT_EQ(ring.available(), 4u);

  std::array<std::int16_t, 8> out {};
  ASSERT_EQ(ring.pop(out.data(), out.size()), 4u);
  EXPECT_EQ(out[0], 3);
  EXPECT_EQ(out[1], 4);
  EXPECT_EQ(out[2], 5);
  EXPECT_EQ(out[3], 6);
}

TEST(MicRingTest, ClearResets) {
  ring_t ring {8};
  push_range(ring, 1, 5);
  ring.clear();
  EXPECT_EQ(ring.available(), 0u);

  std::array<std::int16_t, 4> out {};
  EXPECT_EQ(ring.pop(out.data(), out.size()), 0u);
}

TEST(MicDefaultSourceJsonTest, FormatsNodeName) {
  EXPECT_EQ(platf::pw_mic::default_source_json("moonlight-mic"), R"({"name":"moonlight-mic"})");
}

TEST(MicDefaultSourceJsonTest, EscapesQuotesAndBackslashes) {
  EXPECT_EQ(platf::pw_mic::default_source_json("a\"b\\c"), R"({"name":"a\"b\\c"})");
}

TEST(MicOwnDefaultTest, RecognisesOwnNodeName) {
  EXPECT_TRUE(platf::pw_mic::names_own_node(R"({"name":"moonlight-mic"})"));
  // What the code itself writes must always be recognised again.
  EXPECT_TRUE(platf::pw_mic::names_own_node(platf::pw_mic::default_source_json(platf::pw_mic::kNodeName)));
}

TEST(MicOwnDefaultTest, ToleratesWhitespace) {
  EXPECT_TRUE(platf::pw_mic::names_own_node(R"({ "name" : "moonlight-mic" })"));
  EXPECT_TRUE(platf::pw_mic::names_own_node("{\n  \"name\": \"moonlight-mic\"\n}"));
}

TEST(MicOwnDefaultTest, IgnoresOtherNodes) {
  EXPECT_FALSE(platf::pw_mic::names_own_node(R"({"name":"alsa_input.pci-0000_00_1f.3.analog-stereo"})"));
  // A name that merely starts with ours is somebody else's device.
  EXPECT_FALSE(platf::pw_mic::names_own_node(R"({"name":"moonlight-mic-2"})"));
  EXPECT_FALSE(platf::pw_mic::names_own_node(R"({"name":"my-moonlight-mic"})"));
}

TEST(MicOwnDefaultTest, RejectsEmptyOrMalformedValues) {
  EXPECT_FALSE(platf::pw_mic::names_own_node(""));
  EXPECT_FALSE(platf::pw_mic::names_own_node("moonlight-mic"));
  EXPECT_FALSE(platf::pw_mic::names_own_node(R"({"name":})"));
  EXPECT_FALSE(platf::pw_mic::names_own_node(R"({"name":"moonlight-mic)"));
}

/**
 * Manual integration test against the running PipeWire session; it is disabled by default because it
 * briefly makes "Moonlight Mic" the default audio source. Run it with
 *   test_sunshine --gtest_also_run_disabled_tests --gtest_filter='MicSinkManualTest.*'
 * and inspect `wpctl status` / `pw-record --target moonlight-mic` from another terminal while it runs.
 */
TEST(MicSinkManualTest, DISABLED_PublishesTone) {
  auto sink = platf::mic_sink_create({platf::pw_mic::kSampleRate, platf::pw_mic::kChannels, true});
  if (!sink) {
    GTEST_SKIP() << "PipeWire is not available";
  }

  // Ten seconds of a 440 Hz tone, written in real time as 20 ms frames.
  constexpr std::size_t frame_samples = 960;
  std::array<std::int16_t, frame_samples> frame {};
  std::size_t position = 0;
  for (int n = 0; n < 500; ++n) {
    for (auto &sample : frame) {
      sample = static_cast<std::int16_t>(8000.0 * std::sin(2.0 * M_PI * 440.0 * static_cast<double>(position++) / 48000.0));
    }
    EXPECT_EQ(sink->write(frame.data(), frame.size()), 0);
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
}

#endif  // __linux__
