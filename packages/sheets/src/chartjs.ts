import type { ChartJsConfig, ChartSeries } from "./types.js";

export function toChartJsConfig(series: ChartSeries): ChartJsConfig {
	return {
		type: "bar",
		data: {
			labels: series.labels,
			datasets: [
				{
					label: series.label,
					data: series.values,
					backgroundColor: "#0b6bcb",
					borderColor: "#0758a8",
					borderWidth: 1,
				},
			],
		},
		options: {
			responsive: true,
			maintainAspectRatio: false,
			indexAxis: "y",
			plugins: {
				legend: {
					display: true,
				},
				tooltip: {
					enabled: true,
				},
			},
			scales: {
				x: {
					beginAtZero: true,
					title: {
						display: true,
						text: series.label,
					},
				},
				y: {
					title: {
						display: true,
						text: series.groupLabel,
					},
					ticks: {
						autoSkip: false,
					},
				},
			},
		},
	};
}
