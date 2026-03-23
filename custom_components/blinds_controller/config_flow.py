import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback

from .const import DOMAIN


class BlindsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Blinds Controller."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return BlindsOptionsFlow()

    @callback
    def _get_entity_ids(self, platform="switch"):
        """Return a sorted list of entity IDs for a given platform."""
        return sorted(self.hass.states.async_entity_ids(platform))

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}
        if user_input is not None:
            return self.async_create_entry(
                title=user_input["ent_name"],
                data=user_input,
            )

        all_switches = self._get_entity_ids("switch")
        if not all_switches:
            errors["base"] = "no_switches"
            
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("ent_name"): str,
                    vol.Required("entity_up"): vol.In(all_switches),
                    vol.Required("entity_down"): vol.In(all_switches),
                    vol.Required("time_up"): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Required("time_down"): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional("tilt_open", default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional("tilt_closed", default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional("startup_delay", default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
                    vol.Optional("send_stop_at_end", default=True): bool,
                }
            ),
            errors=errors,
        )


class BlindsOptionsFlow(config_entries.OptionsFlow):
    """Handle an options flow for Blinds Controller."""

    def _opt(self, key, default=None):
        """Helper to get value from options, falling back to data, then default."""
        return self.config_entry.options.get(
            key, self.config_entry.data.get(key, default)
        )

    def _get_entity_ids(self, platform="switch"):
        """Return a sorted list of entity IDs for a given platform."""
        return sorted(self.hass.states.async_entity_ids(platform))

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        all_switches = self._get_entity_ids("switch")

        if not all_switches:
            return self.async_abort(reason="no_switches_found")

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("ent_name", default=self._opt("ent_name")): str,
                    vol.Required("entity_up", default=self._opt("entity_up")): vol.In(all_switches),
                    vol.Required("entity_down", default=self._opt("entity_down")): vol.In(all_switches),
                    vol.Required("time_up", default=self._opt("time_up")): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Required("time_down", default=self._opt("time_down")): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional("tilt_open", default=self._opt("tilt_open", 0.0)): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional("tilt_closed", default=self._opt("tilt_closed", 0.0)): vol.All(vol.Coerce(float), vol.Range(min=0)),
                    vol.Optional("startup_delay", default=self._opt("startup_delay", 0.0)): vol.All(vol.Coerce(float), vol.Range(min=0, max=5)),
                    vol.Optional("send_stop_at_end", default=self._opt("send_stop_at_end", True)): bool,
                }
            ),
        )
