/**
 * @file src/platform/linux/mic_source.h
 * @brief PipeWire virtual microphone fed by a client's microphone stream (declarations; no PipeWire headers).
 */
#pragma once

// standard includes
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <optional>
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
   * @brief A value of the `default.configured.audio.source` metadata key.
   */
  struct configured_value_t {
    std::string type;  ///< Metadata value type, normally `Spa:String:JSON`; empty when none was given.
    std::string value;  ///< Metadata value, normally JSON such as `{"name":"..."}`.

    bool operator==(const configured_value_t &) const = default;
  };

  /**
   * @brief Decides when the virtual microphone may become the default source and what to do at teardown.
   *
   * Pure bookkeeping driven by the metadata events that the PipeWire glue observes; it never talks to PipeWire.
   * The rules it enforces:
   * - The default is only touched once the current value is known: a synchronisation barrier must have completed
   *   after the metadata object was bound. An unknown value is never overwritten.
   * - The value before the switch is remembered as either a configured value or confirmed absence. A value that
   *   names this virtual microphone (left behind by a killed session) counts as absence.
   * - Metadata keeps being observed for the whole session. At teardown the previous state is restored only while
   *   the virtual microphone still owns the default; a source the user chose in the meantime, or an explicit
   *   clearing by the user, is left alone.
   */
  class default_source_policy_t {
  public:
    /**
     * @brief What the glue must do at teardown.
     */
    enum class restore_e {
      nothing,  ///< Leave the default as it is.
      set_previous,  ///< Write `previous` back.
      clear,  ///< Delete the key: no default was configured before the switch.
    };

    /**
     * @brief Result of restore_plan().
     */
    struct restore_plan_t {
      restore_e action;  ///< What to do.
      configured_value_t previous;  ///< The value to write back (only with `set_previous`).
    };

    /**
     * @brief The metadata object was (re)bound; whatever was known about its content is stale now.
     */
    void metadata_bound();

    /**
     * @brief A property event for the configured default source key.
     * @param value The new value, or `std::nullopt` when the key was removed.
     */
    void observe(std::optional<configured_value_t> value);

    /**
     * @brief A sync barrier completed after the metadata object was bound: every event sent so far was observed.
     */
    void synchronized();

    /**
     * @brief Whether the default may be switched now.
     * @return true when the current value is known and no switch has happened yet.
     */
    bool may_switch() const;

    /**
     * @brief The switch is about to be written: remember the value to restore.
     */
    void begin_switch();

    /**
     * @brief What to do at teardown, given everything observed so far.
     */
    restore_plan_t restore_plan() const;

    /**
     * @brief The teardown action was carried out (or was not needed); nothing remains to restore.
     */
    void finished();

    /**
     * @brief Whether a switch was attempted and not yet finished.
     */
    bool switched() const;

    /**
     * @brief The value that was configured before the switch; `std::nullopt` means none was configured.
     */
    std::optional<configured_value_t> previous() const;

    /**
     * @brief The latest observed value of the key; `std::nullopt` means the key is absent.
     */
    std::optional<configured_value_t> current() const;

  private:
    std::optional<configured_value_t> current_;  ///< Latest observed value of the key; empty when absent.
    std::optional<configured_value_t> previous_;  ///< Value before our switch; empty when none was configured.
    bool known_ = false;  ///< `current_` reflects the server state (a barrier completed after binding).
    bool switched_ = false;  ///< A switch was attempted and its restoration has not finished.
    bool used_ = false;  ///< A switch was attempted at some point; a session switches at most once.
  };

  /**
   * @brief Format the value WirePlumber expects for a default-node metadata key.
   * @param node_name PipeWire `node.name`.
   * @return JSON such as `{"name":"moonlight-mic"}`, with `"` and `\` escaped.
   */
  std::string default_source_json(std::string_view node_name);

}  // namespace platf::pw_mic
