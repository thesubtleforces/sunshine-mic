/**
 * @file src/platform/linux/mic_source.cpp
 * @brief PipeWire virtual microphone fed by a client's microphone stream.
 *
 * The PipeWire stream setup and process callback are adapted from Nova
 * (https://github.com/F-e-n-y-x/nova-host, src/platform/linux/mic_write.cpp, GPL-3.0).
 */

// standard includes
#include <algorithm>
#include <format>

// local includes
#include "mic_source.h"
#include "src/platform/common.h"

namespace platf {

  std::unique_ptr<mic_sink_t> mic_sink_create([[maybe_unused]] const mic_sink_config_t &config) {
    return nullptr;
  }

}  // namespace platf

namespace platf::pw_mic {

  ring_t::ring_t(std::size_t capacity_frames):
      buf_(std::max<std::size_t>(capacity_frames, 1)) {
  }

  void ring_t::push(const std::int16_t *samples, std::size_t count) {
    if (!samples || count == 0) {
      return;
    }

    std::lock_guard lock {mutex_};
    const auto cap = buf_.size();

    if (count >= cap) {
      // Only the newest `cap` samples can survive; restart the ring with them.
      std::copy_n(samples + (count - cap), cap, buf_.begin());
      read_pos_ = 0;
      write_pos_ = cap;
      return;
    }

    for (std::size_t i = 0; i < count; ++i) {
      buf_[(write_pos_ + i) % cap] = samples[i];
    }
    write_pos_ += count;

    // Overrun: drop the oldest samples so latency stays bounded by the capacity.
    if (write_pos_ - read_pos_ > cap) {
      read_pos_ = write_pos_ - cap;
    }
  }

  std::size_t ring_t::pop(std::int16_t *out, std::size_t max) {
    std::lock_guard lock {mutex_};
    const auto cap = buf_.size();
    const auto n = std::min<std::size_t>(max, static_cast<std::size_t>(write_pos_ - read_pos_));

    for (std::size_t i = 0; i < n; ++i) {
      out[i] = buf_[(read_pos_ + i) % cap];
    }
    read_pos_ += n;
    return n;
  }

  std::size_t ring_t::available() const {
    std::lock_guard lock {mutex_};
    return static_cast<std::size_t>(write_pos_ - read_pos_);
  }

  std::size_t ring_t::capacity() const {
    return buf_.size();
  }

  void ring_t::clear() {
    std::lock_guard lock {mutex_};
    read_pos_ = write_pos_;
  }

  std::string default_source_json(std::string_view node_name) {
    std::string out {R"({"name":")"};
    for (const char c : node_name) {
      switch (c) {
        case '"':
          out += "\\\"";
          break;
        case '\\':
          out += "\\\\";
          break;
        default:
          if (static_cast<unsigned char>(c) < 0x20) {
            out += std::format("\\u{:04x}", static_cast<unsigned>(static_cast<unsigned char>(c)));
          } else {
            out += c;
          }
      }
    }
    out += R"("})";
    return out;
  }

}  // namespace platf::pw_mic
