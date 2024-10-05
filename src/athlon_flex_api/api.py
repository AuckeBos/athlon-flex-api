"""Athlon Flex API client."""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, ClassVar, TypeVar

from aiohttp import ClientSession
from async_property import async_cached_property
from async_property.cached import AsyncCachedPropertyDescriptor
from pydantic import BaseModel, ConfigDict, Field

from athlon_flex_api import logger
from athlon_flex_api.models.filters.vehicle_cluster_filter import VehicleClusterFilter
from athlon_flex_api.models.filters.vehicle_filter import VehicleFilter
from athlon_flex_api.models.profile import Profile
from athlon_flex_api.models.tax_rate import TaxRate, TaxRates
from athlon_flex_api.models.vehicle import Vehicle
from athlon_flex_api.models.vehicle_cluster import (
    DetailLevel,
    VehicleCluster,
    VehicleClusters,
)

T = TypeVar("T")


class AthlonFlexApi(BaseModel):
    """Athlon Flex API client.

    Exposes functions to load the profile, vehicle clusters, vehicles & vehicle details.
    Uses the aiohttp library to interact with the API. All _async methods can also be
    accessed synchronously by removing the '_async' suffix.

    Attributes:
        email: str The email of the user.
        password: str The password of the user.
        gross_yearly_income: float | None = None The optional (approximate) gross yearly
            income of the user. If provided, the tax rate group will be calculated,
            and added as cookie to the session. The API will then include calculated
            net prices for the user
        apply_loonheffingskorting: bool = True Whether to apply the loonheffingskorting.
            Required to find the tax rate belonging to the gross yearly income.
        TAX_RATES_PAGE_ID: ClassVar[str] = "4ecf5f24-8985-450a-915d-919aa7ffa9df"
            A static page ID, retrieved by manually interacting with the web app.
            The TaxRates endpoint requires a valid page ID to return the tax rates.
            Could not figure out how to get a valid page ID from the API.

    """

    model_config = ConfigDict(
        ignored_types=(AsyncCachedPropertyDescriptor,),
        arbitrary_types_allowed=True,
    )

    email: str
    password: str
    gross_yearly_income: float | None = None
    apply_loonheffingskorting: bool = True
    session: ClientSession = Field(init=False, optional=True, default=None)

    BASE_URL: ClassVar[str] = "https://flex.athlon.com/api/v1"
    TAX_RATES_PAGE_ID: ClassVar[str] = "4ecf5f24-8985-450a-915d-919aa7ffa9df"

    def model_post_init(self, _: Any) -> None:  # noqa: ANN401
        """Initialize the API client.

        Create a new session and login to the API.
        """
        self._await(self._init())

    async def _init(self) -> None:
        """Initialize the API client.

        We have to skip SSL verification because the API uses a self-signed certificate.
        """
        self.session = ClientSession()
        await self._login()
        await self._set_tax_rate_cookie()

    async def _login(self) -> None:
        """Login to the Athlon Flex API.

        Uses username and password to login to the API.
        Connection details are stored in the session.
        """
        endpoint = "MemberLogin"

        response = await self.session.post(
            self._url(endpoint),
            json={"username": self.email, "password": self.password},
            verify_ssl=False,
        )
        response.raise_for_status()

    def _url(self, endpoint: str) -> str:
        result = f"{self.BASE_URL}/{endpoint}"
        logger.debug("Calling %s", result)
        return result

    async def _set_tax_rate_cookie(self) -> None:
        """Set the tax rate cookie in the session."""
        if not self.gross_yearly_income:
            return
        tax_rates = await self.tax_rates_async()
        if tax_rate := tax_rates.rate_of_income(
            self.gross_yearly_income,
            apply_loonheffingskorting=self.apply_loonheffingskorting,
        ):
            self.session.cookie_jar.update_cookies(
                {
                    "TaxGroupLabel": tax_rate.label,
                    "TaxGroup": tax_rate.percentage,
                },
            )
            logger.debug(
                "Tax group set to '%s' (rate %s)",
                tax_rate.label,
                tax_rate.percentage,
            )
        else:
            logger.warning("Could not set tax rate cookie, tax rate not found.")

    @async_cached_property
    async def profile(self) -> Profile:
        """Get the profile of the user."""
        endpoint = "MemberProfile"

        response = await self.session.get(self._url(endpoint), verify_ssl=False)
        response.raise_for_status()

        return Profile(**await response.json())

    async def tax_rates_async(self) -> TaxRates:
        """Load the tax rates registered in Athlon Flex."""
        endpoint = "TaxRates"
        response = await self.session.get(
            self._url(endpoint),
            params={"pageId": self.TAX_RATES_PAGE_ID},
            verify_ssl=False,
        )
        response.raise_for_status()
        return TaxRates(tax_rates=[TaxRate(**rate) for rate in await response.json()])

    async def vehicle_clusters_async(
        self,
        filter_: VehicleClusterFilter | None = None,
        detail_level: DetailLevel = DetailLevel.INCLUDE_VEHICLE_DETAILS,
    ) -> VehicleClusters:
        """Load all clusters that have at least one vehicle available.

        Args:
            filter_: If a filter is not provided, result is filtered based on profile.
                If a filter is provided, result is filtered based on the filter.
                    There exists a special NoFilter subclass to load all clusters.
            detail_level: The level of detail to include in the clusters.

        Returns:
            VehicleClusters: A collection of vehicle clusters.

        """
        filter_ = filter_ or VehicleClusterFilter.from_profile(await self.profile)
        endpoint = "VehicleCluster"
        response = await self.session.get(
            self._url(endpoint),
            params=filter_.to_request_params(),
            verify_ssl=False,
        )
        response.raise_for_status()
        return VehicleClusters(
            vehicle_clusters=await asyncio.gather(
                *[
                    self._apply_detail_level(VehicleCluster(**cluster), detail_level)
                    for cluster in await response.json()
                ],
            ),
        )

    async def _apply_detail_level(
        self,
        cluster: VehicleCluster,
        detail_level: DetailLevel,
    ) -> VehicleCluster:
        """Apply the detail level to the cluster, by loading more data if necessary.

        Args:
            cluster: The cluster to apply the detail level to.
            detail_level: The level of detail to apply.

        Returns:
            VehicleCluster: The cluster with the applied detail level.

        """
        if detail_level >= DetailLevel.INCLUDE_VEHICLES and not cluster.vehicles:
            cluster.vehicles = await self.vehicles_async(cluster)
        if detail_level >= DetailLevel.INCLUDE_VEHICLE_DETAILS:
            cluster.vehicles = await asyncio.gather(
                *[self.vehicle_details_async(vehicle) for vehicle in cluster.vehicles],
            )
        return cluster

    async def vehicles_async(
        self,
        vehicle_cluster: VehicleCluster,
        filter_: VehicleFilter | None = None,
    ) -> list[Vehicle]:
        """Load all available vehicles of a given cluster.

        Args:
            vehicle_cluster: VehicleCluster
                The cluster for which to load the vehicles.
            filter_: VehicleFilter | None = None
                If a filter is not provided, result is filtered based on profile.
                If a filter is provided, result is filtered based on the filter.
                    There exists a special NoFilter subclass to load all vehicles.

        Returns:
            list[Vehicle]: A collection of vehicles of the given make and model.

        """
        filter_ = filter_ or VehicleFilter.from_profile(
            vehicle_cluster.make,
            vehicle_cluster.model,
            await self.profile,
        )
        endpoint = "VehicleVariation"
        response = await self.session.get(
            self._url(endpoint),
            params=filter_.to_request_params(),
            verify_ssl=False,
        )
        response.raise_for_status()
        return [Vehicle(**vehicle) for vehicle in await response.json()]

    async def vehicle_details_async(self, vehicle: Vehicle) -> Vehicle:
        """Load all details of a vehicle.

        Args:
            vehicle: Vehicle
                The vehicle for which to load the details.

        Returns:
            Vehicle: The vehicle with the loaded details.

        """
        endpoint = "Vehicle"
        response = await self.session.get(
            self._url(endpoint),
            params=vehicle.details_request_params(await self.profile),
            verify_ssl=False,
        )
        response.raise_for_status()
        return Vehicle(**await response.json())

    def __getattr__(self, name: str) -> Callable:
        """Allow synchronous access to async methods.

        Any method that ends with '_async' can also be accessed
        synchronously by removing the '_async' suffix.
        """
        if name.endswith("_async"):
            return getattr(super(), name)
        async_name = f"{name}_async"
        if hasattr(self, async_name):
            async_def = getattr(self, async_name)
        else:
            return super().__getattr__(name)
        return lambda *args, **kwargs: self._await(async_def(*args, **kwargs))

    def _await(self, coro: Awaitable[T]) -> T:
        """Run the coroutine in the event loop.

        This makes it possible to run async methods synchronously.
        """
        return asyncio.get_event_loop().run_until_complete(coro)

    def __del__(self) -> None:
        """Automatically close the session when the object is garbage collected."""
        self._await(self.session.close())
