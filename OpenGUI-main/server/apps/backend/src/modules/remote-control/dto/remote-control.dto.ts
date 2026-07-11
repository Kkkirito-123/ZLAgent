import { ApiProperty, ApiPropertyOptional } from "@nestjs/swagger";
import { IsBoolean, IsInt, IsOptional, IsString, Min } from "class-validator";

export class RunRemoteTaskDto {
	@ApiProperty({ description: "Task ID" })
	@IsInt()
	@Min(1)
	taskId: number;

	@ApiPropertyOptional({ description: "Target standby device ID" })
	@IsOptional()
	@IsString()
	deviceId?: string;

	@ApiPropertyOptional({
		description:
			"Whether the backend should dispatch the execution to the standby socket. Local in-app voice entry may set this to false and connect directly from the HTTP response.",
		default: true,
	})
	@IsOptional()
	@IsBoolean()
	dispatch?: boolean;
}

export class DoRemoteTaskDto {
	@ApiProperty({ description: "Task description" })
	@IsString()
	description: string;

	@ApiPropertyOptional({ description: "Optional task name" })
	@IsOptional()
	@IsString()
	taskName?: string;

	@ApiPropertyOptional({ description: "Target standby device ID" })
	@IsOptional()
	@IsString()
	deviceId?: string;

	@ApiPropertyOptional({
		description:
			"Whether the backend should dispatch the execution to the standby socket. Local in-app voice entry may set this to false and connect directly from the HTTP response.",
		default: true,
	})
	@IsOptional()
	@IsBoolean()
	dispatch?: boolean;
}

export class ResumeRemoteExecutionDto {
	@ApiPropertyOptional({ description: "Feedback for a paused or HITL execution" })
	@IsOptional()
	@IsString()
	feedback?: string;
}
