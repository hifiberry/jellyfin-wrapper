-- Route mpv volume to the HiFiBerry hardware volume control instead of
-- software volume. The ALSA card and control are taken from the HiFiBerryOS
-- config-server, so this works with any board that has a hardware volume
-- control. On boards without one, mpv keeps its normal software volume.
-- mpv's own cubic software attenuation is cancelled with volume-gain, so the
-- signal passes at unity and only the hardware attenuates.

local CONFIGSERVER = "http://127.0.0.1:1081/api/v1/soundcard/detect"
local POLL = 2   -- seconds; picks up changes made elsewhere (UI, Spotify)

local msg = require "mp.msg"
local utils = require "mp.utils"
local CARD, CONTROL
local playing = false
local last_alsa = nil   -- last percent we read from / wrote to ALSA
local applying = false  -- set while we change mpv volume ourselves

-- Ask config-server which card and control carry the hardware volume.
local function detect()
    local r = mp.command_native({name = "subprocess", playback_only = false,
        capture_stdout = true, args = {"curl", "-s", "-m", "5", CONFIGSERVER}})
    local j = r.status == 0 and utils.parse_json(r.stdout or "")
    local d = j and j.data
    if not (d and d.card_detected and d.volume_control) then return false end
    CARD = tostring(d.hardware_index or 0)
    CONTROL = d.volume_control
    return true
end

local function amixer(args)
    local cmd = {"amixer", "-M", "-c", CARD}
    for _, a in ipairs(args) do cmd[#cmd + 1] = a end
    local r = mp.command_native({name = "subprocess", args = cmd,
                                 capture_stdout = true, playback_only = false})
    if r.status ~= 0 then msg.warn("amixer failed: " .. table.concat(cmd, " ")) return nil end
    return r.stdout
end

local function read_alsa()
    local out = amixer({"sget", CONTROL})
    local pct = out and out:match("%[(%d+)%%%]")
    return pct and tonumber(pct)
end

local function compensate(v)
    -- software gain is (v/100)^3; undo it so mpv outputs unity
    if v <= 0 then mp.set_property_number("volume-gain", 0) return end
    mp.set_property_number("volume-gain", -60 * math.log(v / 100, 10))
end

local function set_mpv_volume(v)
    applying = true
    mp.set_property_number("volume", v)
    compensate(v)
    applying = false
end

if not detect() then
    msg.info("no hardware volume control reported by config-server, using software volume")
    return
end

mp.set_property_number("volume-gain-max", 150)

mp.observe_property("volume", "number", function(_, v)
    if v == nil then return end
    if v > 100 then mp.set_property_number("volume", 100) return end
    compensate(v)
    if applying or not playing then return end
    local p = math.floor(v + 0.5)
    if p ~= last_alsa then
        amixer({"sset", CONTROL, p .. "%"})
        last_alsa = read_alsa() or p
        msg.info("ALSA " .. CONTROL .. " -> " .. last_alsa .. "%")
    end
end)

mp.register_event("file-loaded", function()
    local a = read_alsa()
    if a then
        last_alsa = a
        set_mpv_volume(a)
        msg.info("volume synced from ALSA: " .. a .. "%")
    end
    playing = true
end)

mp.register_event("end-file", function() playing = false end)

mp.add_periodic_timer(POLL, function()
    if not playing then return end
    local a = read_alsa()
    if a and a ~= last_alsa then
        last_alsa = a
        set_mpv_volume(a)
        msg.info("ALSA changed externally: " .. a .. "%")
    end
end)

msg.info("controlling ALSA card " .. CARD .. ", control '" .. CONTROL .. "'")
