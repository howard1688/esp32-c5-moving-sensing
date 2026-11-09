// SPDX-License-Identifier: Apache-2.0
// C5/IDF 6.0: HE20 SU ESP-NOW sensing + Wi-Fi UDP transport.
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stdatomic.h>
#include <errno.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/event_groups.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_now.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "lwip/sockets.h"
#include "esp_csi_gain_ctrl.h"

#define MAX_CSI 512
#define CONNECTED BIT0
static const char *TAG = "esoc-csi";
static const uint8_t TX_MAC[6] = {0x02,0x45,0x53,0x4f,0xc5,0x01};
static const uint8_t BROADCAST[6] = {255,255,255,255,255,255};
static char node_name[16];
static EventGroupHandle_t network;
static QueueHandle_t samples;
static esp_netif_t *sta;
static uint32_t boot_id;
static atomic_uint captured, queue_drops, udp_errors, sent, send_errors, oversize;
static atomic_bool tx_busy;
static atomic_uint target_hz;
static atomic_uint busy_skips, schedule_skips;
static uint16_t startup_hz;

typedef struct __attribute__((packed)) {
    char magic[4];
    uint8_t version, persist;
    uint16_t hz;
    uint32_t token;
} rate_command_t;
_Static_assert(sizeof(rate_command_t) == 12, "Rate command size mismatch");

static bool valid_rate(uint16_t hz)
{
    static const uint16_t rates[] = {50, 100, 200, 250, 500, 1000};
    for (size_t i = 0; i < sizeof(rates) / sizeof(rates[0]); ++i)
        if (rates[i] == hz) return true;
    return false;
}

static uint16_t load_startup_rate(void)
{
    uint16_t hz = CONFIG_CSI_SEND_HZ;
    nvs_handle_t handle;
    if (nvs_open("csi", NVS_READONLY, &handle) == ESP_OK) {
        uint16_t saved;
        if (nvs_get_u16(handle, "rate_hz", &saved) == ESP_OK && valid_rate(saved)) hz = saved;
        nvs_close(handle);
    }
    return hz;
}

static bool save_startup_rate(uint16_t hz)
{
    nvs_handle_t handle;
    if (nvs_open("csi", NVS_READWRITE, &handle) != ESP_OK) return false;
    esp_err_t err = nvs_set_u16(handle, "rate_hz", hz);
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);
    return err == ESP_OK;
}

// Little endian, packed; Python protocol.HEADER must match exactly.
typedef struct __attribute__((packed)) {
    char magic[4];
    uint8_t version, node;
    uint16_t flags;             // bit0: first 4 CSI bytes invalid; bit1: gain valid
    uint32_t boot, seq;
    uint64_t timestamp_us;      // receiver uptime, callback capture time
    uint32_t radio_timestamp_us, queue_drops, udp_errors;
    int8_t rssi, noise_floor;
    uint8_t channel, format, agc;
    int8_t fft;
    uint16_t rx_seq, csi_len, reserved;
    float gain_compensation;
    uint8_t source_mac[6];
    char name[16];
    int8_t data[MAX_CSI];
} sample_t;
_Static_assert(offsetof(sample_t, data) == 74, "Wire header mismatch");

static void wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(network, CONNECTED);
        ESP_LOGW(TAG, "%s disconnected (reason %d); reconnecting", node_name,
                 ((wifi_event_sta_disconnected_t *)data)->reason);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *e = data;
        xEventGroupSetBits(network, CONNECTED);
        ESP_LOGI(TAG, "%s IP " IPSTR " gateway " IPSTR, node_name,
                 IP2STR(&e->ip_info.ip), IP2STR(&e->ip_info.gw));
    }
}

static void init_wifi(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    network = xEventGroupCreate();
    configASSERT(network);
    sta = esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(esp_netif_set_hostname(sta, node_name));
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_RAM));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    uint8_t mac[6];
    memcpy(mac, TX_MAC, 6);
    mac[5] = CONFIG_CSI_NODE_ID;
    ESP_ERROR_CHECK(esp_wifi_set_mac(WIFI_IF_STA, mac));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, wifi_event, NULL));
    wifi_config_t config = { .sta = {
        .ssid = CONFIG_CSI_SSID, .password = CONFIG_CSI_PASSWORD,
        .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        .pmf_cfg = {.capable = true, .required = false},
    }};
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &config));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_band_mode(WIFI_BAND_MODE_5G_ONLY));
    wifi_protocols_t protocols = {.ghz_5g = WIFI_PROTOCOL_11A | WIFI_PROTOCOL_11N | WIFI_PROTOCOL_11AX};
    ESP_ERROR_CHECK(esp_wifi_set_protocols(ESP_MAC_WIFI_STA, &protocols));
    wifi_bandwidths_t bandwidth = {.ghz_5g = WIFI_BW20};
    ESP_ERROR_CHECK(esp_wifi_set_bandwidths(ESP_MAC_WIFI_STA, &bandwidth));
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    ESP_ERROR_CHECK(esp_wifi_connect());
    ESP_LOGI(TAG, "%s connecting to 5 GHz %s, MAC " MACSTR, node_name,
             CONFIG_CSI_SSID, MAC2STR(mac));
}

static void csi_callback(void *ctx, wifi_csi_info_t *info)
{
    if (!info || !info->buf || !info->len ||
        memcmp(info->mac, TX_MAC, 6) || memcmp(info->dmac, BROADCAST, 6) ||
        info->rx_ctrl.cur_bb_format != RX_BB_FORMAT_HE_SU || info->rx_ctrl.channel <= 14 ||
        !info->rx_ctrl.rx_channel_estimate_info_vld)
        return;
    // Only broadcast action frames; exclude A's data uplink to the hotspot.
    if (!info->hdr || (info->hdr[0] & 0xfc) != 0xd0) return;
    uint32_t seq = atomic_fetch_add(&captured, 1);
    if (info->len > MAX_CSI || (info->len & 1)) {
        atomic_fetch_add(&oversize, 1);
        return;
    }
    sample_t s = {0};
    memcpy(s.magic, "CSI5", 4);
    s.version = 1; s.node = CONFIG_CSI_NODE_ID;
    s.flags = info->first_word_invalid ? 1 : 0;
    s.boot = boot_id; s.seq = seq;
    s.timestamp_us = esp_timer_get_time();
    s.radio_timestamp_us = info->rx_ctrl.timestamp;
    s.queue_drops = atomic_load(&queue_drops);
    s.udp_errors = atomic_load(&udp_errors);
    s.rssi = info->rx_ctrl.rssi; s.noise_floor = info->rx_ctrl.noise_floor;
    s.channel = info->rx_ctrl.channel; s.format = info->rx_ctrl.cur_bb_format;
    uint8_t agc = 0; int8_t fft = 0; float gain = 1;
    esp_csi_gain_ctrl_get_rx_gain(&info->rx_ctrl, &agc, &fft);
    static unsigned gain_samples;
    if (gain_samples < 100) {
        esp_csi_gain_ctrl_record_rx_gain(agc, fft);
        ++gain_samples;
    } else if (gain_samples == 100) {
        uint8_t base_agc; int8_t base_fft;
        if (esp_csi_gain_ctrl_get_rx_gain_baseline(&base_agc, &base_fft) == ESP_OK)
            ++gain_samples;
    }
    if (esp_csi_gain_ctrl_get_gain_compensation(&gain, agc, fft) == ESP_OK)
        s.flags |= 2;
    else gain = 1;
    s.agc = agc; s.fft = fft; s.gain_compensation = gain;
    s.rx_seq = info->rx_seq; s.csi_len = info->len;
    memcpy(s.source_mac, info->mac, 6);
    memcpy(s.name, node_name, sizeof(s.name));
    memcpy(s.data, info->buf, info->len);
    if (xQueueSend(samples, &s, 0) != pdTRUE) atomic_fetch_add(&queue_drops, 1);
}

static void send_done(const esp_now_send_info_t *info, esp_now_send_status_t status)
{
    if (status != ESP_NOW_SEND_SUCCESS) atomic_fetch_add(&send_errors, 1);
    atomic_store(&tx_busy, false);
}

static void probe_tick(void *arg)
{
    // Timer callbacks only wake the sender; never transmit from the timer task.
    xTaskNotifyGive((TaskHandle_t)arg);
}

static void probe_task(void *arg)
{
    esp_timer_handle_t timer;
    esp_timer_create_args_t timer_args = {
        .callback = probe_tick, .arg = xTaskGetCurrentTaskHandle(),
        .name = "probe_tick", .skip_unhandled_events = true,
    };
    ESP_ERROR_CHECK(esp_timer_create(&timer_args, &timer));
    uint16_t previous_hz = atomic_load(&target_hz);
    ESP_ERROR_CHECK(esp_timer_start_periodic(timer, 1000000 / previous_hz));
    uint32_t seq = 0;
    for (;;) {
        uint32_t ticks = ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        uint16_t hz = atomic_load(&target_hz);
        if (hz != previous_hz) {
            ESP_ERROR_CHECK(esp_timer_stop(timer));
            ulTaskNotifyTake(pdTRUE, 0);
            previous_hz = hz;
            ESP_ERROR_CHECK(esp_timer_start_periodic(timer, 1000000 / hz));
            continue;
        }
        // Coalesce delayed notifications rather than generating catch-up bursts.
        if (ticks > 1) atomic_fetch_add(&schedule_skips, ticks - 1);
        if (!(xEventGroupGetBits(network) & CONNECTED)) continue;
        if (atomic_load(&tx_busy)) {
            atomic_fetch_add(&busy_skips, 1);
            continue;
        }
        struct { char magic[4]; uint32_t boot, seq; } probe = {{'E','S','O','C'},boot_id,seq++};
        atomic_store(&tx_busy, true);
        esp_err_t err = esp_now_send(BROADCAST, (uint8_t *)&probe, sizeof(probe));
        if (err == ESP_OK) atomic_fetch_add(&sent, 1);
        else { atomic_store(&tx_busy, false); atomic_fetch_add(&send_errors, 1); }
    }
}

static void udp_task(void *arg)
{
    int sock = -1;
    int control_sock = -1;
    int64_t last_status = 0;
    sample_t s;
    for (;;) {
        xEventGroupWaitBits(network, CONNECTED, pdFALSE, pdTRUE, portMAX_DELAY);
        if (sock < 0) {
            sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
            if (sock < 0) { vTaskDelay(pdMS_TO_TICKS(1000)); continue; }
            struct timeval timeout = {.tv_usec = 20000};
            setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
        }
        if (CONFIG_CSI_NODE_ID == 1 && control_sock < 0) {
            control_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
            if (control_sock >= 0) {
                struct sockaddr_in local = {.sin_family = AF_INET,
                    .sin_port = htons(CONFIG_CSI_CONTROL_PORT), .sin_addr.s_addr = htonl(INADDR_ANY)};
                if (bind(control_sock, (struct sockaddr *)&local, sizeof(local)) != 0) {
                    close(control_sock); control_sock = -1;
                }
            }
        }
        esp_netif_ip_info_t ip;
        ESP_ERROR_CHECK(esp_netif_get_ip_info(sta, &ip));
        struct sockaddr_in dest = {.sin_family = AF_INET, .sin_port = htons(CONFIG_CSI_UDP_PORT)};
        dest.sin_addr.s_addr = ip.gw.addr;
        if (strlen(CONFIG_CSI_HOST_IP) && inet_pton(AF_INET, CONFIG_CSI_HOST_IP, &dest.sin_addr) != 1) {
            ESP_LOGE(TAG, "Invalid CSI_HOST_IP"); vTaskDelay(pdMS_TO_TICKS(2000)); continue;
        }
        if (xQueueReceive(samples, &s, pdMS_TO_TICKS(20)) == pdTRUE) {
            size_t bytes = offsetof(sample_t, data) + s.csi_len;
            if (sendto(sock, &s, bytes, 0, (struct sockaddr *)&dest, sizeof(dest)) != bytes) {
                atomic_fetch_add(&udp_errors, 1); close(sock); sock = -1; continue;
            }
        }
        if (control_sock >= 0) {
            uint8_t command_bytes[sizeof(rate_command_t) + 1];
            struct sockaddr_in source;
            socklen_t source_len = sizeof(source);
            int received = recvfrom(control_sock, command_bytes, sizeof(command_bytes), MSG_DONTWAIT,
                                    (struct sockaddr *)&source, &source_len);
            rate_command_t command;
            if (received == sizeof(command)) memcpy(&command, command_bytes, sizeof(command));
            if (received == sizeof(command) && source.sin_addr.s_addr == dest.sin_addr.s_addr &&
                    !memcmp(command.magic, "RATE", 4) &&
                    command.version == 1 && command.persist <= 1) {
                bool valid = valid_rate(command.hz);
                bool saved = false;
                if (valid) {
                    atomic_store(&target_hz, command.hz);
                    if (command.persist) {
                        saved = save_startup_rate(command.hz);
                        if (saved) startup_hz = command.hz;
                    }
                }
                char ack[192];
                int ack_len = snprintf(ack, sizeof(ack),
                    "{\"type\":\"rate_ack\",\"name\":\"%s\",\"token\":%lu,"
                    "\"ok\":%s,\"target_hz\":%u,\"saved\":%s,\"startup_hz\":%u}",
                    node_name, (unsigned long)command.token, valid ? "true" : "false",
                    (unsigned)atomic_load(&target_hz), saved ? "true" : "false", startup_hz);
                sendto(control_sock, ack, ack_len, 0, (struct sockaddr *)&source, source_len);
            }
        }
        if (esp_timer_get_time() - last_status >= 1000000) {
            last_status = esp_timer_get_time();
            uint8_t channel = 0; wifi_second_chan_t secondary;
            esp_wifi_get_channel(&channel, &secondary);
            char status[512];
            int n = snprintf(status, sizeof(status),
                "{\"type\":\"status\",\"name\":\"%s\",\"boot\":%lu,\"channel\":%u,"
                "\"target_hz\":%u,\"startup_hz\":%u,\"control_port\":%d,\"sent\":%u,\"send_errors\":%u,\"captured\":%u,"
                "\"queue_drops\":%u,\"udp_errors\":%u,\"invalid_length\":%u,"
                "\"uptime_us\":%llu,\"busy_skips\":%u,\"schedule_skips\":%u}",
                node_name, (unsigned long)boot_id, channel, (unsigned)atomic_load(&target_hz),
                startup_hz, CONFIG_CSI_CONTROL_PORT,
                atomic_load(&sent), atomic_load(&send_errors), atomic_load(&captured),
                atomic_load(&queue_drops), atomic_load(&udp_errors), atomic_load(&oversize),
                (unsigned long long)last_status, atomic_load(&busy_skips), atomic_load(&schedule_skips));
            if (sendto(sock, status, n, 0, (struct sockaddr *)&dest, sizeof(dest)) < 0)
                atomic_fetch_add(&udp_errors, 1);
            ESP_LOGI(TAG, "%s ch=%u captured=%u sent=%u queue_drop=%u udp_err=%u", node_name,
                     channel, atomic_load(&captured), atomic_load(&sent),
                     atomic_load(&queue_drops), atomic_load(&udp_errors));
        }
    }
}

void app_main(void)
{
    snprintf(node_name, sizeof(node_name), "esp32c5-%d", CONFIG_CSI_NODE_ID);
    boot_id = esp_random();
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase()); err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);
    startup_hz = load_startup_rate();
    atomic_store(&target_hz, startup_hz);
    samples = xQueueCreate(CONFIG_CSI_QUEUE_DEPTH, sizeof(sample_t));
    configASSERT(samples);
    init_wifi();
    xEventGroupWaitBits(network, CONNECTED, pdFALSE, pdTRUE, portMAX_DELAY);
    ESP_ERROR_CHECK(esp_now_init());
    esp_now_peer_info_t peer = {.channel = 0, .ifidx = WIFI_IF_STA, .encrypt = false};
    memcpy(peer.peer_addr, BROADCAST, 6);
    ESP_ERROR_CHECK(esp_now_add_peer(&peer));
    esp_now_rate_config_t rate = {.phymode = WIFI_PHY_MODE_HE20, .rate = WIFI_PHY_RATE_MCS0_LGI};
    ESP_ERROR_CHECK(esp_now_set_peer_rate_config(BROADCAST, &rate));
    if (CONFIG_CSI_NODE_ID == 1) {
        ESP_ERROR_CHECK(esp_now_register_send_cb(send_done));
        configASSERT(xTaskCreate(probe_task, "probe", 4096, NULL, 5, NULL) == pdPASS);
    } else {
        wifi_csi_config_t csi = {.enable = true, .acquire_csi_su = true,
            .acquire_csi_he_stbc_mode = 2, .val_scale_cfg = 0};
        ESP_ERROR_CHECK(esp_wifi_set_promiscuous(true));
        ESP_ERROR_CHECK(esp_wifi_set_csi_config(&csi));
        ESP_ERROR_CHECK(esp_wifi_set_csi_rx_cb(csi_callback, NULL));
        ESP_ERROR_CHECK(esp_wifi_set_csi(true));
    }
    configASSERT(xTaskCreate(udp_task, "udp", 6144, NULL, 4, NULL) == pdPASS);
}
