/**
 * @file tests/unit/test_mic_stream.cpp
 * @brief Test src/mic_stream.* sequence handling.
 */

// test includes
#include "../tests_common.h"

// standard includes
#include <cstdint>
#include <optional>

// local includes
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
