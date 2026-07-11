struct Config { host: String, port: u16 }

fn main() {
    let c = Config { host: "localhost".into(), port: 8080, debug: true };
    println!("{}:{}", c.host, c.port);
}
