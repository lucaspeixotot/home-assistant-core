"""Support for PlayStation 4 consoles."""
import logging
import asyncio

import pyps4_homeassistant.ps4 as pyps4
from pyps4_homeassistant.errors import NotReady

from homeassistant.core import callback
from homeassistant.components.media_player import (
    ENTITY_IMAGE_URL, MediaPlayerDevice)
from homeassistant.components.media_player.const import (
    MEDIA_TYPE_GAME, MEDIA_TYPE_APP, SUPPORT_SELECT_SOURCE,
    SUPPORT_PAUSE, SUPPORT_STOP, SUPPORT_TURN_OFF, SUPPORT_TURN_ON)
from homeassistant.components.ps4 import (
    format_unique_id, load_games, save_games)
from homeassistant.const import (
    CONF_HOST, CONF_NAME, CONF_REGION,
    CONF_TOKEN, STATE_IDLE, STATE_OFF, STATE_PLAYING)
from homeassistant.helpers import device_registry, entity_registry

from .const import (DEFAULT_ALIAS, DOMAIN as PS4_DOMAIN, PS4_DATA,
                    REGIONS as deprecated_regions)

_LOGGER = logging.getLogger(__name__)

SUPPORT_PS4 = SUPPORT_TURN_OFF | SUPPORT_TURN_ON | \
    SUPPORT_PAUSE | SUPPORT_STOP | SUPPORT_SELECT_SOURCE

ICON = 'mdi:playstation'
MEDIA_IMAGE_DEFAULT = None

DEFAULT_RETRIES = 2


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up PS4 from a config entry."""
    config = config_entry
    await async_setup_platform(
        hass, config, async_add_entities, discovery_info=None)


async def async_setup_platform(
        hass, config, async_add_entities, discovery_info=None):
    """Set up PS4 Platform."""
    creds = config.data[CONF_TOKEN]
    device_list = []
    for device in config.data['devices']:
        host = device[CONF_HOST]
        region = device[CONF_REGION]
        name = device[CONF_NAME]
        ps4 = pyps4.Ps4Async(host, creds, device_name=DEFAULT_ALIAS)
        device_list.append(PS4Device(
            config, name, host, region, ps4, creds))
    async_add_entities(device_list, update_before_add=True)


class PS4Device(MediaPlayerDevice):
    """Representation of a PS4."""

    def __init__(self, config, name, host, region, ps4, creds):
        """Initialize the ps4 device."""
        self._entry_id = config.entry_id
        self._ps4 = ps4
        self._host = host
        self._name = name
        self._region = region
        self._creds = creds
        self._state = None
        self._media_content_id = None
        self._media_title = None
        self._media_image = None
        self._media_type = None
        self._source = None
        self._games = {}
        self._source_list = []
        self._retry = 0
        self._disconnected = False
        self._info = None
        self._unique_id = None

    @callback
    def status_callback(self):
        """Handle status callback. Parse status."""
        self._parse_status()

    @callback
    def schedule_update(self):
        """Schedules update with HA."""
        self.async_schedule_update_ha_state()

    @callback
    def subscribe_to_protocol(self):
        """Notify protocol to callback with update changes."""
        self.hass.data[PS4_DATA].protocol.add_callback(
            self._ps4, self.status_callback)

    @callback
    def unsubscribe_to_protocol(self):
        """Notify protocol to remove callback."""
        self.hass.data[PS4_DATA].protocol.remove_callback(
            self._ps4, self.status_callback)

    def check_region(self):
        """Display logger msg if region is deprecated."""
        # Non-Breaking although data returned may be inaccurate.
        if self._region in deprecated_regions:
            _LOGGER.info("""Region: %s has been deprecated.
                            Please remove PS4 integration
                            and Re-configure again to utilize
                            current regions""", self._region)

    async def async_added_to_hass(self):
        """Subscribe PS4 events."""
        self.hass.data[PS4_DATA].devices.append(self)
        self.check_region()

    async def async_update(self):
        """Retrieve the latest data."""
        if self._ps4.ddp_protocol is not None:
            # Request Status with asyncio transport.
            self._ps4.get_status()

            # Don't attempt to connect if entity is connected or if,
            # PS4 is in standby or disconnected from LAN or powered off.
            if not self._ps4.connected and not self._ps4.is_standby and\
                    self._ps4.is_available:
                try:
                    await self._ps4.async_connect()
                except NotReady:
                    pass

        # Try to ensure correct status is set on startup for device info.
        if self._ps4.ddp_protocol is None:
            # Use socket.socket.
            await self.hass.async_add_executor_job(self._ps4.get_status)
            if self._info is None:
                # Add entity to registry.
                await self.async_get_device_info(self._ps4.status)
            self._ps4.ddp_protocol = self.hass.data[PS4_DATA].protocol
            self.subscribe_to_protocol()

        self._parse_status()

    def _parse_status(self):
        """Parse status."""
        status = self._ps4.status

        if status is not None:
            self._games = load_games(self.hass)
            if self._games is not None:
                self._source_list = list(sorted(self._games.values()))
            self._retry = 0
            self._disconnected = False
            if status.get('status') == 'Ok':
                title_id = status.get('running-app-titleid')
                name = status.get('running-app-name')
                if title_id and name is not None:
                    self._state = STATE_PLAYING
                    if self._media_content_id != title_id:
                        self._media_content_id = title_id
                        self._media_title = name
                        self._source = self._media_title
                        self._media_type = None
                        asyncio.ensure_future(
                            self.async_get_title_data(title_id, name))
                else:
                    if self._state != STATE_IDLE:
                        self.idle()
            else:
                if self._state != STATE_OFF:
                    self.state_off()

        elif self._retry > DEFAULT_RETRIES:
            self.state_unknown()
        else:
            self._retry += 1

    def idle(self):
        """Set states for state idle."""
        self.reset_title()
        self._state = STATE_IDLE
        self.schedule_update()

    def state_off(self):
        """Set states for state off."""
        self.reset_title()
        self._state = STATE_OFF
        self.schedule_update()

    def state_unknown(self):
        """Set states for state unknown."""
        self.reset_title()
        self._state = None
        if self._disconnected is False:
            _LOGGER.warning("PS4 could not be reached")
        self._disconnected = True
        self._retry = 0

    def reset_title(self):
        """Update if there is no title."""
        self._media_title = None
        self._media_content_id = None
        self._media_type = None
        self._source = None

    async def async_get_title_data(self, title_id, name):
        """Get PS Store Data."""
        from pyps4_homeassistant.errors import PSDataIncomplete
        app_name = None
        art = None
        media_type = None
        try:
            title = await self._ps4.async_get_ps_store_data(
                name, title_id, self._region)

        except PSDataIncomplete:
            title = None
        except asyncio.TimeoutError:
            title = None
            _LOGGER.error("PS Store Search Timed out")

        else:
            if title is not None:
                app_name = title.name
                art = title.cover_art
                # Assume media type is game if not app.
                if title.game_type != 'App':
                    media_type = MEDIA_TYPE_GAME
                else:
                    media_type = MEDIA_TYPE_APP
            else:
                _LOGGER.error(
                    "Could not find data in region: %s for PS ID: %s",
                    self._region, title_id)

        finally:
            self._media_title = app_name or name
            self._source = self._media_title
            self._media_image = art or None
            self._media_type = media_type

            self.update_list()
            self.schedule_update()

    def update_list(self):
        """Update Game List, Correct data if different."""
        if self._media_content_id in self._games:
            store = self._games[self._media_content_id]
            if store != self._media_title:
                self._games.pop(self._media_content_id)

        if self._media_content_id not in self._games:
            self.add_games(self._media_content_id, self._media_title)
            self._games = load_games(self.hass)

        self._source_list = list(sorted(self._games.values()))

    def add_games(self, title_id, app_name):
        """Add games to list."""
        games = self._games
        if title_id is not None and title_id not in games:
            game = {title_id: app_name}
            games.update(game)
            save_games(self.hass, games)

    async def async_get_device_info(self, status):
        """Set device info for registry."""
        # If cannot get status on startup, assume info from registry.
        if status is None:
            _LOGGER.info("Assuming status from registry")
            e_registry = await entity_registry.async_get_registry(self.hass)
            d_registry = await device_registry.async_get_registry(self.hass)
            for entity_id, entry in e_registry.entities.items():
                if entry.config_entry_id == self._entry_id:
                    self._unique_id = entry.unique_id
                    self.entity_id = entity_id
                    break
            for device in d_registry.devices.values():
                if self._entry_id in device.config_entries:
                    self._info = {
                        'name': device.name,
                        'model': device.model,
                        'identifiers': device.identifiers,
                        'manufacturer': device.manufacturer,
                        'sw_version': device.sw_version
                    }
                    break

        else:
            _sw_version = status['system-version']
            _sw_version = _sw_version[1:4]
            sw_version = "{}.{}".format(_sw_version[0], _sw_version[1:])
            self._info = {
                'name': status['host-name'],
                'model': 'PlayStation 4',
                'identifiers': {
                    (PS4_DOMAIN, status['host-id'])
                },
                'manufacturer': 'Sony Interactive Entertainment Inc.',
                'sw_version': sw_version
            }

            self._unique_id = format_unique_id(self._creds, status['host-id'])

    async def async_will_remove_from_hass(self):
        """Remove Entity from Hass."""
        # Close TCP Transport.
        if self._ps4.connected:
            await self._ps4.close()
        self.hass.data[PS4_DATA].devices.remove(self)

    @property
    def device_info(self):
        """Return information about the device."""
        return self._info

    @property
    def unique_id(self):
        """Return Unique ID for entity."""
        return self._unique_id

    @property
    def entity_picture(self):
        """Return picture."""
        if self._state == STATE_PLAYING and self._media_content_id is not None:
            image_hash = self.media_image_hash
            if image_hash is not None:
                return ENTITY_IMAGE_URL.format(
                    self.entity_id, self.access_token, image_hash)
        return MEDIA_IMAGE_DEFAULT

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def state(self):
        """Return the state of the device."""
        return self._state

    @property
    def icon(self):
        """Icon."""
        return ICON

    @property
    def media_content_id(self):
        """Content ID of current playing media."""
        return self._media_content_id

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        return self._media_type

    @property
    def media_image_url(self):
        """Image url of current playing media."""
        if self._media_content_id is None:
            return MEDIA_IMAGE_DEFAULT
        return self._media_image

    @property
    def media_title(self):
        """Title of current playing media."""
        return self._media_title

    @property
    def supported_features(self):
        """Media player features that are supported."""
        return SUPPORT_PS4

    @property
    def source(self):
        """Return the current input source."""
        return self._source

    @property
    def source_list(self):
        """List of available input sources."""
        return self._source_list

    async def async_turn_off(self):
        """Turn off media player."""
        await self._ps4.standby()

    async def async_turn_on(self):
        """Turn on the media player."""
        self._ps4.wakeup()

    async def async_media_pause(self):
        """Send keypress ps to return to menu."""
        await self.async_send_remote_control('ps')

    async def async_media_stop(self):
        """Send keypress ps to return to menu."""
        await self.async_send_remote_control('ps')

    async def async_select_source(self, source):
        """Select input source."""
        for title_id, game in self._games.items():
            if source.lower().encode(encoding='utf-8') == \
               game.lower().encode(encoding='utf-8') \
               or source == title_id:

                _LOGGER.debug(
                    "Starting PS4 game %s (%s) using source %s",
                    game, title_id, source)

                await self._ps4.start_title(title_id, self._media_content_id)
                return

        _LOGGER.warning(
            "Could not start title. '%s' is not in source list", source)
        return

    async def async_send_command(self, command):
        """Send Button Command."""
        await self.async_send_remote_control(command)

    async def async_send_remote_control(self, command):
        """Send RC command."""
        await self._ps4.remote_control(command)
