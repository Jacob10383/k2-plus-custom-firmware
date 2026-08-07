# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging


def _klog(msg, *args, level=logging.info):
    level("force_stop_homing: " + msg, *args)


class ForceStopHoming:
    def __init__(self, config):
        self.printer = config.get_printer()
        webhooks = self.printer.lookup_object("webhooks")
        webhooks.register_endpoint(
            "force_stop_homing", self._handle_force_stop_homing
        )

    def _get_runtime_state(self):
        toolhead = self.printer.lookup_object("toolhead")
        homing = self.printer.lookup_object("homing", None)
        homing_active = bool(
            homing is not None
            and (
                homing.has_active_homing_session()
                or homing.is_homing_abort_in_progress()
                or getattr(homing, "active_hmove", None) is not None
            ))
        drip_active = bool(
            homing_active and toolhead.special_queuing_state == "Drip")
        return homing, drip_active, homing_active

    def _handle_force_stop_homing(self, web_request):
        homing, drip_active, homing_active = self._get_runtime_state()
        gcode = self.printer.lookup_object("gcode")
        if homing_active:
            try:
                result = homing.request_homing_abort(
                    reason="Webhook force-stop aborted homing",
                    detail={"source": "force_stop_homing_extra"},
                    abort_z_align=True,
                )
                cleanup_pending = bool(result.get("cleanup_pending"))
                gcode.respond_raw(
                    "!! Force stop homing triggered - coordinated abort"
                )
                web_request.send(
                    {
                        "stopped": not cleanup_pending,
                        "accepted": True,
                        "cleanup_pending": cleanup_pending,
                        "message": "Homing abort accepted",
                        "drip_active": drip_active,
                        "homing_active": homing_active,
                        "endpoint": "force_stop_homing",
                    }
                )
                return
            except Exception as err:
                _klog(
                    "coordinated abort failed",
                    level=logging.exception,
                )
                web_request.send(
                    {
                        "stopped": False,
                        "accepted": False,
                        "cleanup_pending": (
                            homing.is_homing_abort_in_progress()
                        ),
                        "message": "Homing abort failed: %s" % (err,),
                        "endpoint": "force_stop_homing",
                    }
                )
                return
        web_request.send(
            {
                "stopped": False,
                "message": "Not currently homing",
                "endpoint": "force_stop_homing",
            }
        )

    def get_status(self, eventtime):
        _, drip_active, homing_active = self._get_runtime_state()
        return {
            "available": True,
            "endpoint": "force_stop_homing",
            "drip_active": drip_active,
            "homing_active": homing_active,
        }


def load_config(config):
    return ForceStopHoming(config)
