// Copyright The OpenTelemetry Authors
// SPDX-License-Identifier: Apache-2.0

use actix_web::{App, HttpServer};
use json_subscriber;
use std::env;
use tracing::info;

mod shipping_service;
use shipping_service::{get_quote, ship_order};

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    json_subscriber::fmt::init();

    let port: u16 = env::var("SHIPPING_PORT")
        .expect("$SHIPPING_PORT is not set")
        .parse()
        .expect("$SHIPPING_PORT is not a valid port");

    let mut ip = "0.0.0.0".to_string();

    if let Ok(ipv6_enabled) = env::var("IPV6_ENABLED") {
        if ipv6_enabled == "true" {
            ip = "[::]".to_string();
            info!("Overwriting Localhost IP:  {ip}");
        }
    }

    let addr = format!("{}:{}", ip, port);
    info!(
        name = "ServerStartedSuccessfully",
        addr = addr.as_str(),
        message = "Shipping service is running"
    );

    HttpServer::new(|| App::new().service(get_quote).service(ship_order))
        .bind(&addr)?
        .run()
        .await
}
