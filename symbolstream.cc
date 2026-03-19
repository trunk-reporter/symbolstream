/*
 * symbolstream_plugin.cc -- Stream raw IMBE voice codec data to a remote server.
 *
 * Mirrors simplestream's config pattern but sends raw IMBE codewords
 * (via voice_codec_data) instead of decoded PCM audio. The receiving
 * server handles decoding, inference, and presentation.
 *
 * Config (in trunk-recorder config.json plugins array):
 *   {
 *     "name": "symbolstream",
 *     "library": "libsymbolstream_plugin",
 *     "streams": [
 *       {
 *         "address": "127.0.0.1",
 *         "port": 9090,
 *         "TGID": 0,
 *         "shortName": "",
 *         "useTCP": true,
 *         "sendJSON": true
 *       }
 *     ]
 *   }
 *
 * Wire format per frame (sendJSON=false):
 *   4 bytes: tgid (uint32_t LE)
 *   4 bytes: src_id (uint32_t LE)
 *   32 bytes: u[0..7] (8 x uint32_t LE) — IMBE codewords
 *   = 40 bytes per frame at 50fps
 *
 * Wire format per frame (sendJSON=true):
 *   4 bytes: JSON length (uint32_t LE)
 *   N bytes: JSON metadata
 *   32 bytes: u[0..7] (8 x uint32_t LE) — IMBE codewords
 *
 * call_start event (sendJSON=true only):
 *   JSON with {"event":"call_start","talkgroup":N,"src":N,...}
 *
 * call_end event (sendJSON=true only):
 *   JSON with {"event":"call_end","talkgroup":N,"duration":N.N,...}
 */

#include "../../trunk-recorder/plugin_manager/plugin_api.h"
#include "../../trunk-recorder/recorders/recorder.h"

#include <boost/dll/alias.hpp>
#include <boost/log/trivial.hpp>
#include <boost/asio.hpp>
#include <boost/foreach.hpp>

using namespace boost::asio;

struct symbol_stream_t {
    long TGID;
    std::string address;
    std::string short_name;
    long port;
    ip::udp::endpoint remote_endpoint;
    ip::tcp::socket *tcp_socket;
    bool sendJSON = false;
    bool tcp = false;
};

static std::vector<symbol_stream_t> symbol_streams;
static io_service symbol_tcp_io_service;

class Symbol_Stream : public Plugin_Api {
    io_service udp_io_service_;
    ip::udp::socket udp_socket_{udp_io_service_};

public:
    Symbol_Stream() {}

    int parse_config(json config_data) override {
        if (!config_data.contains("streams")) {
            BOOST_LOG_TRIVIAL(error) << "[symbolstream] No 'streams' array in config";
            return -1;
        }

        for (json element : config_data["streams"]) {
            symbol_stream_t stream;
            stream.TGID = element.value("TGID", (long)0);
            stream.address = element.value("address", "127.0.0.1");
            stream.port = element.value("port", (long)9090);
            stream.remote_endpoint = ip::udp::endpoint(
                ip::address::from_string(stream.address), stream.port);
            stream.sendJSON = element.value("sendJSON", false);
            stream.tcp = element.value("useTCP", true);
            stream.short_name = element.value("shortName", "");

            BOOST_LOG_TRIVIAL(info) << "[symbolstream] Stream IMBE from TG "
                << stream.TGID << " to " << stream.address << ":"
                << stream.port << (stream.tcp ? " (TCP)" : " (UDP)")
                << (stream.sendJSON ? " +JSON" : "");

            symbol_streams.push_back(stream);
        }
        return 0;
    }

    int start() override {
        /* Open TCP connections */
        for (auto &stream : symbol_streams) {
            if (stream.tcp) {
                stream.tcp_socket = new ip::tcp::socket(symbol_tcp_io_service);
                try {
                    ip::tcp::endpoint ep(ip::address::from_string(stream.address), stream.port);
                    stream.tcp_socket->connect(ep);
                    BOOST_LOG_TRIVIAL(info) << "[symbolstream] TCP connected to "
                        << stream.address << ":" << stream.port;
                } catch (std::exception &e) {
                    BOOST_LOG_TRIVIAL(error) << "[symbolstream] TCP connect failed: " << e.what();
                    delete stream.tcp_socket;
                    stream.tcp_socket = nullptr;
                }
            }
        }

        /* Open UDP socket */
        udp_socket_.open(ip::udp::v4());
        BOOST_LOG_TRIVIAL(info) << "[symbolstream] Plugin started ("
            << symbol_streams.size() << " streams)";
        return 0;
    }

    int stop() override {
        for (auto &stream : symbol_streams) {
            if (stream.tcp && stream.tcp_socket) {
                stream.tcp_socket->close();
                delete stream.tcp_socket;
                stream.tcp_socket = nullptr;
            }
        }
        return 0;
    }

    /* ---- Voice codec data: stream IMBE codewords ---- */

    int voice_codec_data(Call *call, int codec_type, long tgid,
                         uint32_t src_id, const uint32_t *params,
                         int param_count, int errs) override {
        /* Only P25 Phase 1 IMBE for now */
        if (codec_type != 0 || param_count != 8)
            return 0;

        System *sys = call->get_system();
        std::string short_name = call->get_short_name();
        boost::system::error_code error;

        BOOST_FOREACH (auto &stream, symbol_streams) {
            if (!stream.short_name.empty() && stream.short_name != short_name)
                continue;
            if (stream.TGID != 0 && stream.TGID != tgid)
                continue;

            std::vector<boost::asio::const_buffer> send_buffer;

            if (stream.sendJSON) {
                json j = {
                    {"event", "codec_frame"},
                    {"talkgroup", tgid},
                    {"src", src_id},
                    {"codec_type", codec_type},
                    {"errs", errs},
                    {"short_name", short_name},
                };
                std::string js = j.dump();
                uint32_t jlen = js.length();
                send_buffer.push_back(buffer(&jlen, 4));
                send_buffer.push_back(buffer(js));
            } else {
                uint32_t tg32 = (uint32_t)tgid;
                send_buffer.push_back(buffer(&tg32, 4));
                send_buffer.push_back(buffer(&src_id, 4));
            }
            send_buffer.push_back(buffer(params, param_count * sizeof(uint32_t)));

            if (stream.tcp && stream.tcp_socket) {
                try {
                    stream.tcp_socket->send(send_buffer);
                } catch (std::exception &e) {
                    BOOST_LOG_TRIVIAL(debug) << "[symbolstream] TCP send error: " << e.what();
                }
            } else {
                udp_socket_.send_to(send_buffer, stream.remote_endpoint, 0, error);
            }
        }
        return 0;
    }

    /* ---- Call start/end events ---- */

    int call_start(Call *call) override {
        long tgid = call->get_talkgroup();
        std::string short_name = call->get_short_name();
        boost::system::error_code error;

        BOOST_FOREACH (auto &stream, symbol_streams) {
            if (!stream.sendJSON) continue;
            if (!stream.short_name.empty() && stream.short_name != short_name) continue;
            if (stream.TGID != 0 && stream.TGID != tgid) continue;

            json j = {
                {"event", "call_start"},
                {"talkgroup", tgid},
                {"freq", call->get_freq()},
                {"short_name", short_name},
            };
            std::string js = j.dump();
            uint32_t jlen = js.length();

            std::vector<boost::asio::const_buffer> send_buffer;
            send_buffer.push_back(buffer(&jlen, 4));
            send_buffer.push_back(buffer(js));

            if (stream.tcp && stream.tcp_socket) {
                try { stream.tcp_socket->send(send_buffer); }
                catch (...) {}
            } else {
                udp_socket_.send_to(send_buffer, stream.remote_endpoint, 0, error);
            }
        }
        return 0;
    }

    int call_end(Call_Data_t call_info) override {
        boost::system::error_code error;

        BOOST_FOREACH (auto &stream, symbol_streams) {
            if (!stream.sendJSON) continue;
            if (!stream.short_name.empty() && stream.short_name != call_info.short_name) continue;
            if (stream.TGID != 0 && stream.TGID != call_info.talkgroup) continue;

            json j = {
                {"event", "call_end"},
                {"talkgroup", call_info.talkgroup},
                {"src", call_info.source_num},
                {"freq", call_info.freq},
                {"duration", call_info.length},
                {"short_name", call_info.short_name},
                {"error_count", call_info.error_count},
                {"encrypted", call_info.encrypted},
            };
            std::string js = j.dump();
            uint32_t jlen = js.length();

            std::vector<boost::asio::const_buffer> send_buffer;
            send_buffer.push_back(buffer(&jlen, 4));
            send_buffer.push_back(buffer(js));

            if (stream.tcp && stream.tcp_socket) {
                try { stream.tcp_socket->send(send_buffer); }
                catch (...) {}
            } else {
                udp_socket_.send_to(send_buffer, stream.remote_endpoint, 0, error);
            }
        }
        return 0;
    }

    static boost::shared_ptr<Symbol_Stream> create() {
        return boost::shared_ptr<Symbol_Stream>(new Symbol_Stream());
    }
};

BOOST_DLL_ALIAS(
    Symbol_Stream::create,
    create_plugin
)
