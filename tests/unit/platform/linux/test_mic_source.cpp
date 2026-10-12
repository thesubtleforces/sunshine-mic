/**
 * @file tests/unit/platform/linux/test_mic_source.cpp
 * @brief Test src/platform/linux/mic_source.*
 */

#ifdef __linux__

  // standard includes
  #include <array>
  #include <cstddef>
  #include <cstdint>
  #include <string>

  // lib includes
  #include <gtest/gtest.h>

  // local includes
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

#endif  // __linux__
