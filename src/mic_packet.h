/**
 * @file src/mic_packet.h
 * @brief Pure parser for client-to-host microphone UDP datagrams.
 *
 * Wire format, all fields little-endian, as sent by VoidLink and by qiin2333's
 * moonlight-common-c (MicrophoneStream.c) and accepted by Sunshine-Foundation:
 *
 *   legacy, 12 bytes:   u8 flags | u8 type = 0x61   | u16 seq | u32 timestamp | u32 ssrc (0x12345678)
 *   extended, 13 bytes: u8 flags | u16 type = 0x5504 | u16 seq | u32 timestamp | u32 ssrc
 *
 * followed by one Opus packet, or its AES-128-CBC/PKCS#7 ciphertext when the
 * session negotiated SS_ENC_MIC. The mic IV is the audio-stream IV:
 * BE32(avRiKeyId + seq) followed by 12 zero bytes.
 *
 * Parser and IV derivation adapted from Nova (https://github.com/F-e-n-y-x/nova-host,
 * src/mic_packet.h, GPL-3.0).
 */
#pragma once

// standard includes
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>

namespace mic_packet {

  constexpr std::uint8_t kTypeOpus = 0x61;  ///< Legacy 8-bit packet type (MIC_PACKET_TYPE_OPUS).
  constexpr std::uint16_t kTypeExt = 0x5504;  ///< Extended 16-bit packet type (Sunshine protocol extension "Microphone data").
  constexpr std::uint32_t kMagicSsrc = 0x12345678;  ///< SSRC every client packet carries (informational, not used for routing).
  constexpr std::size_t kLegacyHeaderSize = 12;  ///< Legacy header length in bytes.
  constexpr std::size_t kExtHeaderSize = 13;  ///< Extended header length in bytes.
  constexpr std::size_t kMaxDatagram = 1400;  ///< Largest datagram a client sends (MAX_MIC_PACKET_SIZE).
  constexpr std::uint32_t SS_ENC_MIC = 0x08;  ///< Microphone bit of the GameStream encryption flags (not in upstream's Limelight-internal.h).

  /**
   * @brief Header variant of a parsed datagram.
   */
  enum class header_e {
    legacy,  ///< 12-byte header, type 0x61.
    extended,  ///< 13-byte header, type 0x5504.
  };

  /**
   * @brief One parsed mic datagram; `payload` points into the caller's buffer.
   */
  struct parsed_t {
    header_e header;  ///< Which header layout matched.
    std::uint16_t sequence;  ///< Sequence number, decoded from little-endian.
    std::uint32_t timestamp;  ///< Sender timestamp in milliseconds, decoded from little-endian.
    std::uint32_t ssrc;  ///< SSRC, decoded from little-endian.
    std::span<const std::uint8_t> payload;  ///< Opus bytes or ciphertext; never empty.
  };

  namespace detail {
    /**
     * @brief Decode a little-endian 16-bit value.
     */
    inline std::uint16_t le16(const std::uint8_t *p) {
      return static_cast<std::uint16_t>(p[0] | (p[1] << 8));
    }

    /**
     * @brief Decode a little-endian 32-bit value.
     */
    inline std::uint32_t le32(const std::uint8_t *p) {
      return static_cast<std::uint32_t>(p[0]) | (static_cast<std::uint32_t>(p[1]) << 8) |
             (static_cast<std::uint32_t>(p[2]) << 16) | (static_cast<std::uint32_t>(p[3]) << 24);
    }
  }  // namespace detail

  /**
   * @brief Parse a mic datagram header.
   * @param data Datagram bytes.
   * @param size Datagram length in bytes.
   * @return The parsed datagram, or `std::nullopt` when it is not a mic packet or carries no payload.
   */
  inline std::optional<parsed_t> parse(const std::uint8_t *data, std::size_t size) {
    if (!data) {
      return std::nullopt;
    }

    // Extended first: a legacy header always has byte 1 == 0x61, which can never be the
    // low byte (0x04) of the extended type, so the two layouts cannot overlap.
    if (size > kExtHeaderSize && detail::le16(data + 1) == kTypeExt) {
      return parsed_t {
        header_e::extended,
        detail::le16(data + 3),
        detail::le32(data + 5),
        detail::le32(data + 9),
        {data + kExtHeaderSize, size - kExtHeaderSize},
      };
    }

    if (size > kLegacyHeaderSize && data[1] == kTypeOpus) {
      return parsed_t {
        header_e::legacy,
        detail::le16(data + 2),
        detail::le32(data + 4),
        detail::le32(data + 8),
        {data + kLegacyHeaderSize, size - kLegacyHeaderSize},
      };
    }

    return std::nullopt;
  }

  /**
   * @brief Build the 16-byte AES-CBC IV for one mic packet.
   * @param av_ri_key_id Numeric `rikeyid` of the launch session (`session::audio.avRiKeyId`).
   * @param sequence Packet sequence number from the header.
   * @return The IV: BE32(av_ri_key_id + sequence) followed by 12 zero bytes.
   */
  inline std::array<std::uint8_t, 16> iv_for(std::uint32_t av_ri_key_id, std::uint16_t sequence) {
    std::array<std::uint8_t, 16> iv {};
    const std::uint32_t v = av_ri_key_id + sequence;
    iv[0] = static_cast<std::uint8_t>(v >> 24);
    iv[1] = static_cast<std::uint8_t>(v >> 16);
    iv[2] = static_cast<std::uint8_t>(v >> 8);
    iv[3] = static_cast<std::uint8_t>(v);
    return iv;
  }

}  // namespace mic_packet
