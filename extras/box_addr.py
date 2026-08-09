# Copyright (C) 2026  36573259+Jacob10383@users.noreply.github.com
# This file may be distributed under the terms of the GNU GPLv3 license.
"""CFS discovery and stable, capacity-aware address assignment."""

import logging
from dataclasses import dataclass


ADDRESS_WEDGE_WARNING = (
    "If a CFS shows ID '0' after address assignment, it is wedged and requires "
    "a power cycle.")
MAX_ADDRESSES = 4


def _klog(msg, *args, level=logging.info):
    level("box_addr: " + msg, *args)


@dataclass(frozen=True)
class AutoAddressResult:
    """Reachable and persisted identities plus nonfatal enumeration errors."""

    online: dict
    known: dict
    errors: tuple


class AutoAddressManager:
    """Discover boxes while treating offline identities as soft reservations."""

    def __init__(self, target_count, known_ids=None):
        self.target_count = target_count
        self._known_input = dict(known_ids or {})

    def enumerate(self, client):
        """Find the requested number of boxes without deriving topology from it."""
        errors = []
        known = dict(self._known_input)
        _klog(
            'enumeration start target=%d known=%s', self.target_count,
            {address: uniid.hex() for address, uniid in sorted(known.items())}, level=logging.info)
        for uniid, addresses in self._known_addresses_by_id(known).items():
            if len(addresses) > 1:
                errors.append(
                    "box %s has multiple persisted addresses: %s"
                    % (uniid.hex(), ",".join(str(item) for item in addresses)))
        online = {}
        occupied = set()

        for address in range(1, MAX_ADDRESSES + 1):
            if len(online) >= self.target_count:
                break
            reply = self._call(errors, "query address %d" % address,
                               client.query, address)
            if reply is None:
                _klog(
                    'A2 query address=%d response=none', address, level=logging.info)
                continue
            uniid = reply.uniid
            _klog(
                'A2 query address=%d uid=%s', address, uniid.hex(), level=logging.info)
            occupied.add(address)
            self._remember(known, address, uniid)
            online[address] = uniid

        # Discovery returns one unaddressed box at a time. Each successful
        # assignment removes it from discovery, so target_count remains a
        # strict performance bound even if a faulty client repeats a response.
        seen_discoveries = set()
        while len(online) < self.target_count:
            reply = self._call(errors, "discover", client.discover)
            if reply is None:
                _klog('A1 discovery response=none', level=logging.info)
                break
            uniid = reply.uniid
            _klog('A1 discovered uid=%s', uniid.hex(), level=logging.info)
            if uniid in seen_discoveries:
                errors.append("discover repeated box %s" % uniid.hex())
                break
            seen_discoveries.add(uniid)

            preferred = sorted(
                address for address, saved in known.items()
                if saved == uniid and address not in occupied)
            target = (
                preferred[0] if preferred
                else self._first_free_address(occupied))
            if target is None:
                errors.append("all four CFS addresses are online")
                break

            _klog(
                'A0 assign uid=%s address=%d source=%s', uniid.hex(), target,
                "persisted" if preferred else "first-free", level=logging.info)
            assigned = self._call(
                errors, "assign box %s to address %d" % (uniid.hex(), target),
                client.assign, uniid, target)
            if assigned is None:
                errors.append(
                    "assign address %d returned no valid response" % target)
                errors.append(ADDRESS_WEDGE_WARNING)
                break

            verified = self._call(
                errors, "verify address %d" % target, client.query, target)
            if verified is None:
                errors.append("address %d verification gave no response" % target)
                break
            if verified.uniid != uniid:
                errors.append(
                    "address %d verification returned the wrong box identity"
                    % target)
                break

            _klog(
                'A0 verified uid=%s address=%d', uniid.hex(), target, level=logging.info)
            self._remember(known, target, uniid)
            online[target] = uniid
            occupied.add(target)

        result = AutoAddressResult(
            online=dict(sorted(online.items())),
            known=dict(sorted(known.items())),
            errors=tuple(errors),
        )
        _klog(
            'enumeration complete online=%s known=%s errors=%s', {address: uniid.hex()
             for address, uniid in result.online.items()},
            {address: uniid.hex()
             for address, uniid in result.known.items()},
            list(result.errors) or "none", level=logging.info)
        return result

    @staticmethod
    def _known_addresses_by_id(known):
        result = {}
        for address, uniid in known.items():
            result.setdefault(uniid, []).append(address)
        return result

    @staticmethod
    def _remember(known, address, uniid):
        for previous, saved in list(known.items()):
            if saved == uniid and previous != address:
                del known[previous]
        known[address] = uniid

    @staticmethod
    def _first_free_address(occupied):
        for address in range(1, MAX_ADDRESSES + 1):
            if address not in occupied:
                return address
        return None

    @staticmethod
    def _call(errors, label, method, *args):
        try:
            return method(*args)
        except Exception as exc:
            errors.append("%s failed: %s" % (label, exc))
            return None
