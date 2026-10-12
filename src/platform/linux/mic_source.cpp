/**
 * @file src/platform/linux/mic_source.cpp
 * @brief PipeWire virtual microphone fed by a client's microphone stream.
 *
 * The PipeWire stream setup and process callback are adapted from Nova
 * (https://github.com/F-e-n-y-x/nova-host, src/platform/linux/mic_write.cpp, GPL-3.0).
 */

// standard includes
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <format>
#include <memory>
#include <optional>
#include <utility>

// local includes
#include "mic_source.h"
#include "src/platform/common.h"

#ifdef SUNSHINE_BUILD_PIPEWIRE_MIC
  // lib includes
  #include <pipewire/extensions/metadata.h>
  #include <pipewire/pipewire.h>
  #include <spa/param/audio/format-utils.h>
  #include <spa/utils/result.h>

  // local includes
  #include "src/logging.h"
  #include "src/utility.h"
#endif

using namespace std::literals;

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

  bool names_own_node(std::string_view value) {
    constexpr std::string_view key = "\"name\"";

    const auto key_pos = value.find(key);
    if (key_pos == std::string_view::npos) {
      return false;
    }

    auto pos = key_pos + key.size();
    const auto skip_whitespace = [&]() {
      while (pos < value.size() && (value[pos] == ' ' || value[pos] == '\t' || value[pos] == '\n' || value[pos] == '\r')) {
        ++pos;
      }
    };

    skip_whitespace();
    if (pos >= value.size() || value[pos] != ':') {
      return false;
    }
    ++pos;

    skip_whitespace();
    if (pos >= value.size() || value[pos] != '"') {
      return false;
    }
    ++pos;

    // The name must match exactly: it has to be followed by the closing quote.
    const auto rest = value.substr(pos);
    return rest.size() > kNodeName.size() && rest.substr(0, kNodeName.size()) == kNodeName && rest[kNodeName.size()] == '"';
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

#ifdef SUNSHINE_BUILD_PIPEWIRE_MIC

  namespace {

    /**
     * @brief PipeWire-backed virtual microphone ("Moonlight Mic").
     *
     * Creates an `Audio/Source` stream node fed from a ring buffer and, optionally, makes it the
     * default audio source through PipeWire's `default` metadata object (the same key
     * `wpctl set-default` writes). Destruction removes the node and restores the previous default.
     */
    class pipewire_mic_sink_t: public platf::mic_sink_t {
    public:
      ~pipewire_mic_sink_t() override {
        shutdown();
      }

      /**
       * @brief Connect to PipeWire and create the source node.
       * @param config Device parameters; only 48 kHz mono is supported.
       * @return true when the virtual microphone is ready for samples.
       */
      bool init(const mic_sink_config_t &config) {
        if (config.sample_rate != kSampleRate || config.channels != kChannels) {
          BOOST_LOG(error) << "mic: unsupported virtual microphone format "sv << config.sample_rate << " Hz x"sv << config.channels;
          return false;
        }

        pw_init(nullptr, nullptr);

        loop_ = pw_thread_loop_new("moonlight-mic", nullptr);
        if (!loop_) {
          BOOST_LOG(error) << "mic: pw_thread_loop_new failed"sv;
          return false;
        }

        context_ = pw_context_new(pw_thread_loop_get_loop(loop_), nullptr, 0);
        if (!context_) {
          BOOST_LOG(error) << "mic: pw_context_new failed"sv;
          return false;
        }

        if (pw_thread_loop_start(loop_) != 0) {
          BOOST_LOG(error) << "mic: pw_thread_loop_start failed"sv;
          return false;
        }

        pw_thread_loop_lock(loop_);
        auto unlock_guard = util::fail_guard([this]() {
          pw_thread_loop_unlock(loop_);
        });

        core_ = pw_context_connect(context_, nullptr, 0);
        if (!core_) {
          BOOST_LOG(warning) << "mic: cannot connect to PipeWire: "sv << std::strerror(errno);
          return false;
        }

        static const pw_core_events core_events = []() {
          pw_core_events ev = {};
          ev.version = PW_VERSION_CORE_EVENTS;
          ev.done = &pipewire_mic_sink_t::on_core_done;
          return ev;
        }();
        pw_core_add_listener(core_, &core_listener_, &core_events, this);

        auto props = pw_properties_new(
          PW_KEY_MEDIA_TYPE,
          "Audio",
          PW_KEY_MEDIA_CATEGORY,
          "Playback",
          PW_KEY_MEDIA_ROLE,
          "Communication",
          PW_KEY_MEDIA_CLASS,
          "Audio/Source",
          PW_KEY_NODE_NAME,
          kNodeName.data(),
          PW_KEY_NODE_DESCRIPTION,
          kNodeDescription.data(),
          PW_KEY_NODE_VIRTUAL,
          "true",
          PW_KEY_NODE_LATENCY,
          "960/48000",
          nullptr);

        static const pw_stream_events stream_events = []() {
          pw_stream_events ev = {};
          ev.version = PW_VERSION_STREAM_EVENTS;
          ev.state_changed = &pipewire_mic_sink_t::on_stream_state_changed;
          ev.process = &pipewire_mic_sink_t::on_process;
          return ev;
        }();

        stream_ = pw_stream_new(core_, kNodeDescription.data(), props);
        if (!stream_) {
          BOOST_LOG(error) << "mic: pw_stream_new failed"sv;
          return false;
        }
        pw_stream_add_listener(stream_, &stream_listener_, &stream_events, this);

        std::uint8_t param_buffer[1024];
        spa_pod_builder builder = SPA_POD_BUILDER_INIT(param_buffer, sizeof(param_buffer));
        spa_audio_info_raw audio_format = {};
        audio_format.format = SPA_AUDIO_FORMAT_S16;
        audio_format.rate = kSampleRate;
        audio_format.channels = kChannels;
        audio_format.position[0] = SPA_AUDIO_CHANNEL_MONO;
        const spa_pod *params[1];
        params[0] = spa_format_audio_raw_build(&builder, SPA_PARAM_EnumFormat, &audio_format);

        const int err = pw_stream_connect(
          stream_,
          PW_DIRECTION_OUTPUT,
          PW_ID_ANY,
          static_cast<pw_stream_flags>(PW_STREAM_FLAG_MAP_BUFFERS | PW_STREAM_FLAG_RT_PROCESS),
          params,
          1
        );
        if (err < 0) {
          BOOST_LOG(error) << "mic: pw_stream_connect failed: "sv << spa_strerror(err);
          return false;
        }

        if (config.set_default_source) {
          switch_default_source();
        }

        BOOST_LOG(info) << "mic: PipeWire source '"sv << kNodeDescription << "' created (S16 48 kHz mono)"sv;
        return true;
      }

      int write(const std::int16_t *pcm, std::size_t frames) override {
        ring_.push(pcm, frames);
        return 0;
      }

    private:
      /**
       * @brief Wait until the server has processed everything sent so far (loop must be locked).
       * @return false on timeout.
       */
      bool roundtrip() {
        done_ = false;
        pending_sync_ = pw_core_sync(core_, PW_ID_CORE, 0);
        for (int waits = 0; !done_ && waits < 3; ++waits) {
          if (pw_thread_loop_timed_wait(loop_, 1) != 0) {
            break;
          }
        }
        return done_;
      }

      /**
       * @brief Remember the current default source and point it at the virtual microphone (loop must be locked).
       */
      void switch_default_source() {
        registry_ = pw_core_get_registry(core_, PW_VERSION_REGISTRY, 0);
        if (!registry_) {
          BOOST_LOG(warning) << "mic: cannot get the PipeWire registry; the default source will not be switched"sv;
          return;
        }

        static const pw_registry_events registry_events = []() {
          pw_registry_events ev = {};
          ev.version = PW_VERSION_REGISTRY_EVENTS;
          ev.global = &pipewire_mic_sink_t::on_registry_global;
          return ev;
        }();
        pw_registry_add_listener(registry_, &registry_listener_, &registry_events, this);

        // The registry announces every global during the first roundtrip; the `default`
        // metadata object is bound from the callback.
        roundtrip();
        if (!metadata_) {
          BOOST_LOG(warning) << "mic: PipeWire 'default' metadata object not found (WirePlumber not running?); '"sv
                             << kNodeDescription << "' will not become the default source"sv;
          return;
        }

        // The bound metadata object announces its current properties asynchronously.
        roundtrip();

        const auto json = default_source_json(kNodeName);
        if (pw_metadata_set_property(metadata_, PW_ID_CORE, kDefaultSourceKey.data(), "Spa:String:JSON", json.c_str()) < 0) {
          BOOST_LOG(warning) << "mic: could not set the default audio source"sv;
          return;
        }
        default_set_ = true;

        BOOST_LOG(info) << "mic: default audio source set to '"sv << kNodeName << "' (previous: "sv
                        << (previous_default_ ? previous_default_->second : "none"s) << ')';
      }

      /**
       * @brief Restore the default source and tear everything down.
       */
      void shutdown() {
        if (loop_) {
          pw_thread_loop_lock(loop_);

          if (default_set_ && metadata_) {
            if (previous_default_) {
              const auto &[type, value] = *previous_default_;
              pw_metadata_set_property(metadata_, PW_ID_CORE, kDefaultSourceKey.data(), type.empty() ? nullptr : type.c_str(), value.c_str());
            } else {
              // There was no configured default before; clear the key so WirePlumber chooses again.
              pw_metadata_set_property(metadata_, PW_ID_CORE, kDefaultSourceKey.data(), nullptr, nullptr);
            }
            default_set_ = false;
            if (core_) {
              roundtrip();  // flush the change before the connection goes away
            }
            BOOST_LOG(info) << "mic: default audio source restored"sv;
          }

          if (stream_) {
            spa_hook_remove(&stream_listener_);
            pw_stream_destroy(stream_);
            stream_ = nullptr;
          }
          if (metadata_) {
            spa_hook_remove(&metadata_listener_);
            pw_proxy_destroy(reinterpret_cast<pw_proxy *>(metadata_));
            metadata_ = nullptr;
          }
          if (registry_) {
            spa_hook_remove(&registry_listener_);
            pw_proxy_destroy(reinterpret_cast<pw_proxy *>(registry_));
            registry_ = nullptr;
          }
          if (core_) {
            spa_hook_remove(&core_listener_);
            pw_core_disconnect(core_);
            core_ = nullptr;
          }

          pw_thread_loop_unlock(loop_);
          pw_thread_loop_stop(loop_);
        }
        if (context_) {
          pw_context_destroy(context_);
          context_ = nullptr;
        }
        if (loop_) {
          pw_thread_loop_destroy(loop_);
          loop_ = nullptr;
        }
      }

      static void on_core_done(void *data, uint32_t id, int seq) {
        auto *self = static_cast<pipewire_mic_sink_t *>(data);
        if (id == PW_ID_CORE && seq == self->pending_sync_) {
          self->done_ = true;
          pw_thread_loop_signal(self->loop_, false);
        }
      }

      static void on_registry_global(void *data, uint32_t id, [[maybe_unused]] uint32_t permissions, const char *type, [[maybe_unused]] uint32_t version, const spa_dict *props) {
        auto *self = static_cast<pipewire_mic_sink_t *>(data);
        if (self->metadata_ || !type || !props || std::strcmp(type, PW_TYPE_INTERFACE_Metadata) != 0) {
          return;
        }

        const char *name = spa_dict_lookup(props, PW_KEY_METADATA_NAME);
        if (!name || std::strcmp(name, "default") != 0) {
          return;
        }

        static const pw_metadata_events metadata_events = []() {
          pw_metadata_events ev = {};
          ev.version = PW_VERSION_METADATA_EVENTS;
          ev.property = &pipewire_mic_sink_t::on_metadata_property;
          return ev;
        }();

        self->metadata_ = static_cast<pw_metadata *>(pw_registry_bind(self->registry_, id, type, PW_VERSION_METADATA, 0));
        if (self->metadata_) {
          pw_metadata_add_listener(self->metadata_, &self->metadata_listener_, &metadata_events, self);
        }
      }

      static int on_metadata_property(void *data, uint32_t subject, const char *key, const char *type, const char *value) {
        auto *self = static_cast<pipewire_mic_sink_t *>(data);

        // Only the configured default before our own write matters; later changes are ignored.
        if (self->default_set_ || subject != PW_ID_CORE) {
          return 0;
        }

        if (!key) {
          self->previous_default_.reset();  // the whole subject was cleared
        } else if (std::strcmp(key, kDefaultSourceKey.data()) == 0) {
          if (value && names_own_node(value)) {
            // A session that was killed left our node configured as the default. Restoring that would
            // pin a dead device forever, so treat it as "no default was configured".
            BOOST_LOG(warning) << "mic: ignoring a stale default audio source left behind by an earlier session: "sv << value;
            self->previous_default_.reset();
          } else if (value) {
            self->previous_default_ = std::pair<std::string, std::string> {type ? type : "", value};
          } else {
            self->previous_default_.reset();
          }
        }
        return 0;
      }

      static void on_stream_state_changed([[maybe_unused]] void *data, [[maybe_unused]] pw_stream_state old_state, pw_stream_state state, const char *error) {
        if (state == PW_STREAM_STATE_ERROR) {
          BOOST_LOG(warning) << "mic: PipeWire stream error: "sv << (error ? error : "unknown");
        } else {
          BOOST_LOG(debug) << "mic: PipeWire stream state: "sv << pw_stream_state_as_string(state);
        }
      }

      static void on_process(void *data) {
        auto *self = static_cast<pipewire_mic_sink_t *>(data);

        pw_buffer *b = pw_stream_dequeue_buffer(self->stream_);
        if (!b) {
          return;
        }

        spa_data &d = b->buffer->datas[0];
        if (!d.data) {
          pw_stream_queue_buffer(self->stream_, b);
          return;
        }

        constexpr std::uint32_t stride = sizeof(std::int16_t) * kChannels;
        std::uint32_t max_frames = d.maxsize / stride;
        if (b->requested) {
          max_frames = std::min<std::uint32_t>(max_frames, static_cast<std::uint32_t>(b->requested));
        }

        auto *out = static_cast<std::int16_t *>(d.data);
        const auto filled = self->ring_.pop(out, max_frames);
        // Underrun: emit silence so the node keeps producing a steady clock.
        std::fill(out + filled, out + max_frames, std::int16_t {0});

        d.chunk->offset = 0;
        d.chunk->stride = stride;
        d.chunk->size = max_frames * stride;
        pw_stream_queue_buffer(self->stream_, b);
      }

      pw_thread_loop *loop_ = nullptr;  ///< Thread loop running all PipeWire callbacks.
      pw_context *context_ = nullptr;  ///< PipeWire context bound to `loop_`.
      pw_core *core_ = nullptr;  ///< Connection to the PipeWire daemon.
      pw_registry *registry_ = nullptr;  ///< Registry used to find the `default` metadata object.
      pw_metadata *metadata_ = nullptr;  ///< Bound `default` metadata object, if present.
      pw_stream *stream_ = nullptr;  ///< The `Audio/Source` stream.
      spa_hook core_listener_ {};  ///< Core event listener.
      spa_hook registry_listener_ {};  ///< Registry event listener.
      spa_hook metadata_listener_ {};  ///< Metadata event listener.
      spa_hook stream_listener_ {};  ///< Stream event listener.
      int pending_sync_ = 0;  ///< Sequence of the outstanding core sync.
      bool done_ = false;  ///< Set by the core `done` event for `pending_sync_`.
      bool default_set_ = false;  ///< Whether this object changed the default source.
      std::optional<std::pair<std::string, std::string>> previous_default_;  ///< (type, value) of the default before the switch; empty when none was configured.
      ring_t ring_ {kRingCapacityFrames};  ///< PCM waiting to be pulled by PipeWire.
    };

  }  // namespace

#endif  // SUNSHINE_BUILD_PIPEWIRE_MIC

}  // namespace platf::pw_mic

namespace platf {

  std::unique_ptr<mic_sink_t> mic_sink_create([[maybe_unused]] const mic_sink_config_t &config) {
#ifdef SUNSHINE_BUILD_PIPEWIRE_MIC
    auto sink = std::make_unique<pw_mic::pipewire_mic_sink_t>();
    if (!sink->init(config)) {
      return nullptr;
    }
    return sink;
#else
    return nullptr;
#endif
  }

}  // namespace platf
