trait Shape {
    fn area(&self) -> f64;
}
struct Circle { r: f64 }
impl Shape for Circle {}
fn main() {}
