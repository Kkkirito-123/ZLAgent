import { Injectable, OnModuleDestroy } from "@nestjs/common";
import { AppLogger } from "../log";
import type { DeviceAppInfo, StandbySocket } from "./types";

/**
 *
 */
@Injectable()
export class StandbySocketService implements OnModuleDestroy {
	/** deviceId → socket */
	private readonly devices = new Map<string, StandbySocket>();

	private readonly socketToDevice = new Map<string, string>();

	constructor(private readonly logger: AppLogger) {
		this.logger.setContext(StandbySocketService.name);
	}

	onModuleDestroy() {
		this.devices.clear();
		this.socketToDevice.clear();
	}

	/**
	 */
	registerDevice(
		deviceId: string,
		socket: StandbySocket,
		deviceName?: string,
		installedApps?: DeviceAppInfo[],
	): void {

		if (this.devices.has(deviceId)) {
			const old = this.devices.get(deviceId)!;
			this.logger.warn(
				`Replacing standby connection for device ${deviceId}: old=${old.id}, new=${socket.id}`,
			);
			this.socketToDevice.delete(old.id);
			try {
				old.disconnect(true);
			} catch (e) {
				this.logger.warn(
					`Failed to disconnect old standby socket ${old.id}: ${(e as Error).message}`,
				);
			}
		}

			socket.deviceId = deviceId;
			socket.deviceName = deviceName;
			socket.installedApps = this.normaliseApps(installedApps);
			this.devices.set(deviceId, socket);
		this.socketToDevice.set(socket.id, deviceId);

		this.logger.log(
			`Device registered: ${deviceId}${deviceName ? ` (${deviceName})` : ""}, socket=${socket.id}`,
		);
	}

	/**
	 */
	unregisterDevice(socketId: string): string | undefined {
		const deviceId = this.socketToDevice.get(socketId);
		if (deviceId) {
			this.devices.delete(deviceId);
			this.socketToDevice.delete(socketId);
			this.logger.log(`Device unregistered: ${deviceId}`);
		}
		return deviceId;
	}

	/**
	 */
	getOnlineDevice(): StandbySocket | null {
		for (const [, socket] of this.devices) {
			if (socket.connected) return socket;
		}
		return null;
	}

	getOnlineDeviceById(deviceId: string): StandbySocket | null {
		const socket = this.devices.get(deviceId);
		return socket?.connected ? socket : null;
	}

	/**
	 */
	getOnlineDevices(): Array<{
		deviceId: string;
		deviceName?: string;
		appCount?: number;
	}> {
		const result: Array<{ deviceId: string; deviceName?: string; appCount?: number }> = [];
		for (const [deviceId, socket] of this.devices) {
			if (socket.connected) {
				result.push({
					deviceId,
					deviceName: socket.deviceName,
					appCount: socket.installedApps?.length ?? 0,
				});
			}
		}
		return result;
	}

	getDeviceApps(deviceId: string): DeviceAppInfo[] | null {
		const socket = this.devices.get(deviceId);
		if (!socket?.connected) return null;
		return socket.installedApps ?? [];
	}

	/**
	 */
	hasOnlineDevice(): boolean {
		return this.getOnlineDevice() !== null;
	}

	private normaliseApps(apps?: DeviceAppInfo[]): DeviceAppInfo[] {
		if (!Array.isArray(apps)) return [];
		const seen = new Set<string>();
		const result: DeviceAppInfo[] = [];
		for (const app of apps) {
			const appName = String(app?.appName ?? "").trim();
			const packageName = String(app?.packageName ?? "").trim();
			if (!appName || !packageName || seen.has(packageName)) continue;
			seen.add(packageName);
			const activityName = String(app?.activityName ?? "").trim();
			result.push({
				appName,
				packageName,
				...(activityName ? { activityName } : {}),
			});
		}
		return result.sort((a, b) => a.appName.localeCompare(b.appName));
	}
}
