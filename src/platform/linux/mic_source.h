/**
 * @file src/platform/linux/mic_source.h
 * @brief PipeWire virtual microphone fed by a client's microphone stream (declarations; no PipeWire headers).
 */
#pragma once

// standard includes
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <string_view>
#include <vector>

namespace platf::pw_mic {

  constexpr std::uint32_t kSampleRate = 48000;  ///< Sample rate of the virtual source in Hz.
  constexpr int kChannels = 1;  ///< Channel count of the virtual source.
  constexpr std::size_t kRingCapacityFrames = kSampleRate / 5;  ///< Ring capacity in frames (200 ms); older audio is overwritten.
  constexpr std::string_view kNodeName = "moonlight-mic";  ///< PipeWire `node.name` of the virtual source.
  constexpr std::string_view kNodeDescription = "Moonlight Mic";  ///< PipeWire `node.description` shown in audio settings.
  constexpr std::string_view kDefaultSourceKey = "default.configured.audio.source";  ///< Key in PipeWire's `default` metadata that `wpctl set-default` writes.

  /**
   * @brief Bounded PCM ring buffer with overwrite-oldest semantics.
   *
   * The producer (UDP thread) and consumer (PipeWire realtime thread) are different threads;
   * all access is serialised by a mutex. When the producer outruns the consumer the oldest
   * samples are discarded, which bounds latency instead of letting backlog build up.
   */
  class ring_t {
  public:
    /**
     * @brief Construct a ring.
     * @param capacity_frames Maximum number of buffered samples.
     */
    explicit ring_t(std::size_t capacity_frames);

    /**
     * @brief Append samples, discarding the oldest buffered samples when the ring would overflow.
     * @param samples Samples to append.
     * @param count Number of samples.
     */
    void push(const std::int16_t *samples, std::size_t count);

    /**
     * @brief Remove up to `max` samples from the front of the ring.
     * @param out Destination buffer.
     * @param max Capacity of `out` in samples.
     * @return Number of samples copied; the caller zero-fills the remainder.
     */
    std::size_t pop(std::int16_t *out, std::size_t max);

    /**
     * @brief Number of buffered samples.
     */
    std::size_t available() const;

    /**
     * @brief Maximum number of buffered samples.
     */
    std::size_t capacity() const;

    /**
     * @brief Discard all buffered samples.
     */
    void clear();

  private:
    std::vector<std::int16_t> buf_;  ///< Circular storage.
    std::uint64_t read_pos_ = 0;  ///< Monotonic count of samples consumed.
    std::uint64_t write_pos_ = 0;  ///< Monotonic count of samples produced.
    mutable std::mutex mutex_;  ///< Guards the positions and storage.
  };

  /**
   * @brief Whether a `default.configured.audio.source` value names this virtual microphone.
   *
   * A session that was killed, or lost its PipeWire connection, can leave our own node configured as the
   * default. That is not a user preference and must never be restored as the "previous" default.
   *
   * @param value Metadata value, normally JSON such as `{"name":"moonlight-mic"}`; may be empty or malformed.
   * @return true when the value's `name` is exactly kNodeName.
   */
  bool names_own_node(std::string_view value);

  /**
   * @brief Format the value WirePlumber expects for a default-node metadata key.
   * @param node_name PipeWire `node.name`.
   * @return JSON such as `{"name":"moonlight-mic"}`, with `"` and `\` escaped.
   */
  std::string default_source_json(std::string_view node_name);

}  // namespace platf::pw_mic
