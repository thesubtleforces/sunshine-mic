/**
 * @file tests/unit/test_mic_packet.cpp
 * @brief Test src/mic_packet.h
 */

// test includes
#include "../tests_common.h"

// standard includes
#include <array>
#include <cstdint>
#include <vector>

// local includes
#include <src/mic_packet.h>

namespace {
  void put_le16(std::vector<std::uint8_t> &out, std::uint16_t v) {
    out.push_back(static_cast<std::uint8_t>(v));
    out.push_back(static_cast<std::uint8_t>(v >> 8));
  }

  void put_le32(std::vector<std::uint8_t> &out, std::uint32_t v) {
    for (int i = 0; i < 4; ++i) {
      out.push_back(static_cast<std::uint8_t>(v >> (8 * i)));
    }
  }

  /**
   * @brief A datagram as moonlight-common-c's MicrophoneStream.c sends it: 12-byte little-endian header + payload.
   */
  std::vector<std::uint8_t> legacy_packet(std::uint16_t seq, const std::vector<std::uint8_t> &payload, std::uint32_t timestamp = 123456) {
    std::vector<std::uint8_t> out {0x00, mic_packet::kTypeOpus};
    put_le16(out, seq);
    put_le32(out, timestamp);
    put_le32(out, mic_packet::kMagicSsrc);
    out.insert(out.end(), payload.begin(), payload.end());
    return out;
  }

  /**
   * @brief The extended Foundation variant: u8 flags | u16 type 0x5504 | u16 seq | u32 timestamp | u32 ssrc.
   */
  std::vector<std::uint8_t> extended_packet(std::uint16_t seq, const std::vector<std::uint8_t> &payload, std::uint32_t timestamp = 654321) {
    std::vector<std::uint8_t> out {0x00};
    put_le16(out, mic_packet::kTypeExt);
    put_le16(out, seq);
    put_le32(out, timestamp);
    put_le32(out, mic_packet::kMagicSsrc);
    out.insert(out.end(), payload.begin(), payload.end());
    return out;
  }
}  // namespace

TEST(MicPacketTest, ParsesLegacyHeaderLittleEndian) {
  const auto bytes = legacy_packet(0x0102, {0xAA, 0xBB, 0xCC}, 0x0A0B0C0D);
  // Document the wire order: the sequence number is little-endian.
  ASSERT_EQ(bytes[2], 0x02);
  ASSERT_EQ(bytes[3], 0x01);

  const auto parsed = mic_packet::parse(bytes.data(), bytes.size());
  ASSERT_TRUE(parsed.has_value());
  EXPECT_EQ(parsed->header, mic_packet::header_e::legacy);
  EXPECT_EQ(parsed->sequence, 0x0102);
  EXPECT_EQ(parsed->timestamp, 0x0A0B0C0Du);
  EXPECT_EQ(parsed->ssrc, mic_packet::kMagicSsrc);
  ASSERT_EQ(parsed->payload.size(), 3u);
  EXPECT_EQ(parsed->payload[0], 0xAA);
  EXPECT_EQ(parsed->payload[2], 0xCC);
  // The payload must alias the caller's buffer right after the 12-byte header.
  EXPECT_EQ(parsed->payload.data(), bytes.data() + mic_packet::kLegacyHeaderSize);
}

TEST(MicPacketTest, ParsesExtendedHeader) {
  const auto bytes = extended_packet(0xBEEF, {1, 2, 3, 4});
  const auto parsed = mic_packet::parse(bytes.data(), bytes.size());
  ASSERT_TRUE(parsed.has_value());
  EXPECT_EQ(parsed->header, mic_packet::header_e::extended);
  EXPECT_EQ(parsed->sequence, 0xBEEF);
  EXPECT_EQ(parsed->timestamp, 654321u);
  EXPECT_EQ(parsed->ssrc, mic_packet::kMagicSsrc);
  ASSERT_EQ(parsed->payload.size(), 4u);
  EXPECT_EQ(parsed->payload.data(), bytes.data() + mic_packet::kExtHeaderSize);
}

TEST(MicPacketTest, RejectsHeaderWithoutPayload) {
  const auto legacy = legacy_packet(1, {});
  ASSERT_EQ(legacy.size(), mic_packet::kLegacyHeaderSize);
  EXPECT_FALSE(mic_packet::parse(legacy.data(), legacy.size()).has_value());

  const auto extended = extended_packet(1, {});
  ASSERT_EQ(extended.size(), mic_packet::kExtHeaderSize);
  EXPECT_FALSE(mic_packet::parse(extended.data(), extended.size()).has_value());
}

TEST(MicPacketTest, RejectsShortTruncatedAndNull) {
  const auto bytes = legacy_packet(7, {0x11, 0x22, 0x33});
  EXPECT_FALSE(mic_packet::parse(nullptr, 64).has_value());
  EXPECT_FALSE(mic_packet::parse(bytes.data(), 0).has_value());
  EXPECT_FALSE(mic_packet::parse(bytes.data(), 5).has_value());
  EXPECT_FALSE(mic_packet::parse(bytes.data(), 11).has_value());
}

TEST(MicPacketTest, RejectsGarbageTypeByte) {
  // An all-zero datagram has the shape of a legacy PING, not of a mic packet.
  const std::vector<std::uint8_t> zeros(16, 0x00);
  EXPECT_FALSE(mic_packet::parse(zeros.data(), zeros.size()).has_value());

  auto bytes = legacy_packet(3, {0x01, 0x02});
  bytes[1] = 0x60;  // one off MIC_PACKET_TYPE_OPUS
  EXPECT_FALSE(mic_packet::parse(bytes.data(), bytes.size()).has_value());
}

TEST(MicPacketTest, AcceptsMaxDatagram) {
  const std::vector<std::uint8_t> payload(mic_packet::kMaxDatagram - mic_packet::kLegacyHeaderSize, 0x5A);
  const auto bytes = legacy_packet(9, payload);
  ASSERT_EQ(bytes.size(), mic_packet::kMaxDatagram);
  const auto parsed = mic_packet::parse(bytes.data(), bytes.size());
  ASSERT_TRUE(parsed.has_value());
  EXPECT_EQ(parsed->payload.size(), 1388u);
}

TEST(MicPacketTest, IvForMatchesAudioIvScheme) {
  // BE32(avRiKeyId + seq) followed by 12 zero bytes, identical to the host->client audio IV.
  const auto iv = mic_packet::iv_for(0x01020304, 5);
  const std::array<std::uint8_t, 16> expected {0x01, 0x02, 0x03, 0x09};
  EXPECT_EQ(iv, expected);

  // The 32-bit sum wraps; the 16-bit sequence is added as-is.
  const auto wrapped = mic_packet::iv_for(0xFFFFFFFF, 2);
  const std::array<std::uint8_t, 16> expected_wrapped {0x00, 0x00, 0x00, 0x01};
  EXPECT_EQ(wrapped, expected_wrapped);

  const auto high_seq = mic_packet::iv_for(0, 0xFFFF);
  const std::array<std::uint8_t, 16> expected_high {0x00, 0x00, 0xFF, 0xFF};
  EXPECT_EQ(high_seq, expected_high);
}
